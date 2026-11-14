"""Regression / smoke tests for the pure decision logic in the v4 entrypoint
(`rootfs/usr/bin/energy_optimizer.py`).

The `eo/` package (v5 engine) already has its own extensive suite; this file
covers the still-shipping v4 reactive engine's derived-value and decision
functions — the ones reached every cycle when `v5_engine_enabled` is false
(the default). They are exercised without MQTT/HA/InfluxDB: the module is
imported with `ENERGY_OPT_TESTING=1` so it has no import-time side effects
(no `/data` mkdir), and `ENERGY_OPT_DATA` points at a temp dir.

Run: `pytest tests/test_main_logic.py`
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

_SRC = (
    pathlib.Path(__file__).parent.parent
    / "rootfs" / "usr" / "bin" / "energy_optimizer.py"
)


@pytest.fixture(scope="session")
def mod(tmp_path_factory):
    os.environ["ENERGY_OPT_TESTING"] = "1"
    os.environ["ENERGY_OPT_DATA"] = str(tmp_path_factory.mktemp("data"))
    spec = importlib.util.spec_from_file_location("energy_optimizer_app", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── import-time hygiene ──────────────────────────────────────────────────────

def test_module_imports_without_side_effects(mod):
    # The whole point: importing the shipped entrypoint must not require /data
    # to be writable nor talk to HA. If we got here the import succeeded.
    assert mod.ADD_ON_VERSION
    assert callable(mod.calculate_optimal_soc)


def test_version_matches_config_yaml(mod):
    import yaml
    cfg = yaml.safe_load((pathlib.Path(_SRC).parents[3] / "config.yaml").read_text())
    assert mod.ADD_ON_VERSION == cfg["version"]


# ── tariff period classification ─────────────────────────────────────────────

def test_current_tariff_periods(mod, monkeypatch):
    from datetime import datetime

    class _DT(datetime):
        _now = datetime(2026, 6, 17, 11, 0, 0)  # Wednesday 11:00 -> peak

        @classmethod
        def now(cls, tz=None):
            return cls._now

    monkeypatch.setattr(mod, "datetime", _DT)

    _DT._now = datetime(2026, 6, 17, 11, 0, 0)   # Wed 11h -> peak
    assert mod.current_tariff()["period"] == "peak"

    _DT._now = datetime(2026, 6, 17, 3, 0, 0)    # Wed 03h -> valley
    assert mod.current_tariff()["period"] == "valley"

    _DT._now = datetime(2026, 6, 17, 16, 0, 0)   # Wed 16h -> mid
    assert mod.current_tariff()["period"] == "mid"

    _DT._now = datetime(2026, 6, 20, 11, 0, 0)   # Saturday -> valley all day
    assert mod.current_tariff()["period"] == "valley"


# ── entity auto-discovery scoring ────────────────────────────────────────────

def test_score_entity_domain_gate(mod):
    # A switch can never score for a sensor-only role.
    st = {"entity_id": "switch.battery_soc",
          "attributes": {"friendly_name": "battery soc"}}
    assert mod._score_entity(st, "battery_soc") == 0


def test_score_entity_device_class_unit_and_keywords(mod):
    st = {
        "entity_id": "sensor.battery_state_of_capacity",
        "attributes": {
            "friendly_name": "Battery SOC",
            "device_class": "battery",
            "unit_of_measurement": "%",
        },
    }
    # device_class(+50) + unit(+25/40) + keyword hits -> clearly positive,
    # and higher than a same-domain sensor with no matching hints.
    score = mod._score_entity(st, "battery_soc")
    other = mod._score_entity(
        {"entity_id": "sensor.kitchen_humidity",
         "attributes": {"friendly_name": "kitchen humidity"}},
        "battery_soc",
    )
    assert score > other
    assert score >= 50


def test_get_entity_candidates_ranks_best_first(mod):
    states = [
        {"entity_id": "sensor.kitchen_humidity",
         "attributes": {"friendly_name": "Kitchen humidity"}},
        {"entity_id": "sensor.battery_state_of_capacity",
         "attributes": {"friendly_name": "Battery SOC",
                        "device_class": "battery",
                        "unit_of_measurement": "%"}},
    ]
    cands = mod.get_entity_candidates("battery_soc", states)
    assert cands[0]["entity_id"] == "sensor.battery_state_of_capacity"


# ── solar geometry proxy (pure) ──────────────────────────────────────────────

def test_solar_proxy_is_zero_at_night(mod):
    assert mod._solar_proxy_for_hour(2, 6) == 0.0   # 02:00 -> sun below horizon


def test_solar_proxy_peaks_at_summer_noon(mod):
    noon_summer = mod._solar_proxy_for_hour(13, 6)
    noon_winter = mod._solar_proxy_for_hour(13, 12)
    assert 0.0 < noon_winter < noon_summer <= 1.0


# ── storm detection ──────────────────────────────────────────────────────────

def test_is_storm_forecast_current_condition(mod, monkeypatch):
    monkeypatch.setattr(mod, "cfg", lambda *a, **k: "weather.aemet")
    monkeypatch.setattr(mod, "ha_state",
                        lambda e: {"state": "lightning", "attributes": {}})
    assert mod.is_storm_forecast() is True


def test_is_storm_forecast_from_future_entry(mod, monkeypatch):
    monkeypatch.setattr(mod, "cfg", lambda *a, **k: "weather.aemet")
    monkeypatch.setattr(mod, "ha_state", lambda e: {
        "state": "sunny",
        "attributes": {"forecast": [{"condition": "sunny"},
                                    {"condition": "pouring"}]},
    })
    assert mod.is_storm_forecast() is True


def test_is_storm_forecast_clear(mod, monkeypatch):
    monkeypatch.setattr(mod, "cfg", lambda *a, **k: "weather.aemet")
    monkeypatch.setattr(mod, "ha_state", lambda e: {
        "state": "sunny", "attributes": {"forecast": [{"condition": "cloudy"}]},
    })
    assert mod.is_storm_forecast() is False


# ── optimal-SOC valley charging target (issue #5 / #8 regressions) ───────────

def _patch_soc_inputs(mod, monkeypatch, *, base_kw=0.4, correction=1.0,
                      health=("bill_reducer",)):
    """Make calculate_optimal_soc deterministic: no InfluxDB, no history."""
    monkeypatch.setattr(mod, "_compute_solar_correction_factor", lambda: correction)
    monkeypatch.setattr(mod, "_get_avg_night_consumption_kw", lambda: base_kw)
    monkeypatch.setattr(mod, "_solar_historical_baseline", lambda d: None)
    monkeypatch.setattr(mod, "_daylight_hours_today", lambda: None)

    def _cfg(key, default=None):
        return {"battery_capacity_kwh": 10.0,
                "battery_health_mode": health[0]}.get(key, default)
    monkeypatch.setattr(mod, "cfg", _cfg)


def test_optimal_soc_scales_with_peak_hours(mod, monkeypatch):
    # Issue #5: a 16h-peak tariff must demand more stored energy than the
    # Spanish 8h-peak default for the same base load and no solar.
    _patch_soc_inputs(mod, monkeypatch)
    sensors = {"solar_tomorrow": 0.0, "temp_outdoor": 15.0}

    spain = mod.calculate_optimal_soc(sensors, {"peak_hours": list(range(8))})
    ukraine = mod.calculate_optimal_soc(sensors, {"peak_hours": list(range(16))})

    assert ukraine["peak_h_count"] == 16
    assert spain["peak_h_count"] == 8
    assert ukraine["target_soc"] > spain["target_soc"]


def test_optimal_soc_respects_health_mode_floor(mod, monkeypatch):
    # Issue #8: with bill_reducer the floor is 10 %, not a hardcoded 30 %.
    _patch_soc_inputs(mod, monkeypatch, health=("bill_reducer",))
    # Lots of solar, tiny base load -> battery_needed ~0 -> clamps to floor.
    sensors = {"solar_tomorrow": 50.0, "temp_outdoor": 20.0}
    res = mod.calculate_optimal_soc(sensors, {"peak_hours": list(range(8))})
    assert res["health_mode_min"] == 10
    assert res["target_soc"] == 10


def test_optimal_soc_cold_weather_raises_target(mod, monkeypatch):
    _patch_soc_inputs(mod, monkeypatch)
    warm = mod.calculate_optimal_soc(
        {"solar_tomorrow": 0.0, "temp_outdoor": 20.0},
        {"peak_hours": list(range(8))},
    )
    cold = mod.calculate_optimal_soc(
        {"solar_tomorrow": 0.0, "temp_outdoor": 0.0},
        {"peak_hours": list(range(8))},
    )
    assert cold["temp_adj_kwh"] > warm["temp_adj_kwh"]
    assert cold["target_soc"] >= warm["target_soc"]


# ── daily summary return value (v5.0.20 NameError regression) ────────────────

def test_send_daily_summary_returns_action_count(mod, monkeypatch, tmp_path):
    """Until v5.0.20 the 23:00 job raised `NameError: action_details` on the
    final return — after both notifications had already gone out, so the bug
    was invisible from the user's inbox and only showed in the add-on log."""
    monkeypatch.setattr(mod, "_load_savings", lambda: {
        "total_eur_saved": 1.0, "total_kwh_avoided_peak": 2.0, "since": "2026-01-01"})
    monkeypatch.setattr(mod, "read_sensors", lambda: {"battery_soc": 50.0})
    monkeypatch.setattr(mod, "_summarize_day_narrative",
                        lambda dec, sens: ["⚡ line one.", "🏊 line two."])
    monkeypatch.setattr(mod, "ha_service", lambda *a, **k: True)
    monkeypatch.setattr(mod, "DECISIONS_FILE", tmp_path / "no_such_decisions.json")

    def _cfg(key, default=None):
        return {"notify_email_daily_enabled": False,
                "notify_telegram_daily_enabled": False}.get(key, default)
    monkeypatch.setattr(mod, "cfg", _cfg)

    res = mod.send_daily_summary()
    assert res["actions"] == 2
    assert res["cycles"] == 0


# ── charge hysteresis (v5.0.20 flapping regression) ─────────────────────────

def test_charge_hysteresis_holds_until_band_is_cleared(mod, monkeypatch):
    """Prod 2026-08-26 07:15-09:00: reach 95 %, hand back to self-consumption,
    drop to 93 %, re-engage, repeat every 15-min cycle. The Schmitt trigger
    must keep the charge off until SOC drops a full band below the target."""
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                        5 if k == "battery_charge_hysteresis_pct" else d)
    mod._BATT_CHARGE_ENGAGED = False

    assert mod._should_engage_charge(60, 95) is True    # far below -> charge
    assert mod._should_engage_charge(93, 95) is True    # latched: keep going
    assert mod._should_engage_charge(95, 95) is False   # target reached -> stop
    assert mod._should_engage_charge(94, 95) is False   # inside band -> hold
    assert mod._should_engage_charge(91, 95) is False   # still inside band
    assert mod._should_engage_charge(90, 95) is True    # band cleared -> re-engage


def test_charge_hysteresis_zero_restores_bang_bang(mod, monkeypatch):
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                        0 if k == "battery_charge_hysteresis_pct" else d)
    mod._BATT_CHARGE_ENGAGED = False

    assert mod._should_engage_charge(95, 95) is False
    assert mod._should_engage_charge(94, 95) is True


def test_decide_battery_does_not_flap_at_ceiling(mod, monkeypatch):
    """End-to-end through decide_battery: the exact prod sequence of SOC
    readings must not produce a charge/self_consumption alternation."""
    _patch_soc_inputs(mod, monkeypatch, base_kw=2.3)

    def _cfg(key, default=None):
        return {"battery_capacity_kwh": 10.0,
                "battery_health_mode": "bill_reducer",
                "battery_charge_hysteresis_pct": 5,
                "battery_emergency_threshold": 10,
                "battery_low_threshold": 30,
                "battery_storm_threshold": 80}.get(key, default)
    monkeypatch.setattr(mod, "cfg", _cfg)
    monkeypatch.setattr(mod, "is_storm_forecast", lambda: False)
    monkeypatch.setattr(mod, "_battery_charge_power_w", lambda default_w=2000: 2000)
    mod._BATT_CHARGE_ENGAGED = False

    tariff = {"period": "valley", "prices": {"peak": 0.22, "mid": 0.15, "valley": 0.11},
              "peak_hours": list(range(8))}
    sensors = {"solar_tomorrow": 8.2, "temp_outdoor": 20.0}

    # SOC track observed on prod: climbs to the 95 % ceiling, then sags.
    actions = []
    for soc in (90, 93, 95, 94, 93, 92, 91, 90):
        sensors["battery_soc"] = soc
        actions.append(mod.decide_battery(sensors, tariff, {})["action"])

    # Charge up to the ceiling, then a single clean hand-off, no alternation
    # until the band is cleared at 90 %.
    assert actions == ["charge", "charge", "self_consumption", "self_consumption",
                       "self_consumption", "self_consumption", "self_consumption",
                       "charge"]


# ── _hp_zone_decision: controlled_hvac (comfort/savings weight + overrides) ──
# These cover the "controlled heat/cool device" tier added for heat pumps
# whose own firmware ignores a plain setpoint outside manual mode (e.g.
# Vaillant Genia Air in auto/day/night mode — see genia-air-ha memory). A
# zone with controlled_hvac unset/false must behave exactly as before.

def _hvac_zone(mod, monkeypatch, *, summer=False, surplus=False, **zone_overrides):
    monkeypatch.setattr(mod, "_is_summer", lambda: summer)
    monkeypatch.setattr(mod, "_has_surplus_power", lambda soc, pwr: surplus)
    zone = {
        "name": "Test zone", "temp_heat": "number.heat_setpoint",
        "temp_cool": "number.cool_setpoint",
        "sched": {str(h): "minimum" for h in range(24)},
    }
    zone.update(zone_overrides)
    sensors = {"battery_soc": 50.0, "battery_power": 0.0, "temp_indoor": 19.0,
               "temp_zone_0": 19.0}
    return zone, sensors


def test_hp_zone_decision_legacy_unaffected_by_new_fields(mod, monkeypatch):
    """controlled_hvac unset → identical to pre-feature behaviour, even if
    override_heat/temp_emergency/weight are present in the zone dict (e.g.
    leftover config from toggling the checkbox off)."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False,
                                override_heat="number.quick_veto",
                                comfort_savings_weight=0, temp_emergency=5.0)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["entity"] == "number.heat_setpoint"      # NOT the override
    assert d["target"] == 18.0                          # plain temp_min default
    assert d["reason"] == "Minimum heating [Test zone]"


def test_hp_zone_decision_controlled_weight_zero_is_pure_savings(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                temp_comfort=22.0, temp_min=18.0,
                                comfort_savings_weight=0)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 18.0


def test_hp_zone_decision_controlled_weight_hundred_is_pure_comfort(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                temp_comfort=22.0, temp_min=18.0,
                                comfort_savings_weight=100)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 22.0


def test_hp_zone_decision_controlled_weight_blends_linearly(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                temp_comfort=22.0, temp_min=18.0,
                                comfort_savings_weight=50)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 20.0


def test_hp_zone_decision_controlled_writes_to_override_entity(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                override_heat="number.genia_air_quick_veto_setpoint")
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["entity"] == "number.genia_air_quick_veto_setpoint"


def test_hp_zone_decision_controlled_falls_back_without_override_entity(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["entity"] == "number.heat_setpoint"


# ── _hp_zone_decision: override_mode_entity (2026-11-14) ────────────────────
# Some heat pumps (confirmed live on a Vaillant Genia Air) silently ignore
# any override setpoint write while their own mode select stays on "auto" —
# `override_mode_entity`/`override_mode_option` let the apply loop flip that
# select before writing the override, instead of pretending a plain number
# write is always enough.

def test_hp_zone_decision_mode_entity_present_when_controlled_and_configured(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                override_heat="number.genia_air_manual_setpoint",
                                override_mode_entity="select.genia_air_heating_operation_mode")
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["mode_entity"] == "select.genia_air_heating_operation_mode"
    assert d["mode_option"] == "day"  # default


def test_hp_zone_decision_mode_option_can_be_customized(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                override_mode_entity="select.foo",
                                override_mode_option="manual")
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["mode_option"] == "manual"


def test_hp_zone_decision_mode_entity_absent_when_not_controlled(mod, monkeypatch):
    """A zone with controlled_hvac unset must never try to flip a mode select,
    even if override_mode_entity is left over in its config from toggling the
    checkbox off — matches the legacy-unaffected guarantee."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False,
                                override_mode_entity="select.genia_air_heating_operation_mode")
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["mode_entity"] == ""


def test_hp_zone_decision_mode_entity_absent_when_not_configured(mod, monkeypatch):
    """controlled_hvac=True but no override_mode_entity configured → "" (the
    heat pump doesn't need this quirk, e.g. one whose override already works
    without leaving 'auto')."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True)
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["mode_entity"] == ""


def test_hp_zone_decision_mode_entity_present_on_emergency_branch(mod, monkeypatch):
    """The emergency early-return must also carry mode_entity/mode_option so
    the apply loop can still flip the mode select before writing the
    emergency target."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                override_mode_entity="select.genia_air_heating_operation_mode")
    sensors = {"battery_soc": 50.0, "battery_power": 0.0}  # no temp_zone_0, no temp_indoor
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["reason"].startswith("Emergency")
    assert d["mode_entity"] == "select.genia_air_heating_operation_mode"


def test_hp_zone_decision_comfort_and_surplus_tiers_ignore_weight(mod, monkeypatch):
    # Weight only tunes the "otherwise min/inactive" tier — comfort/surplus
    # stay exactly at their configured values regardless of the dial.
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                temp_comfort=22.0, temp_surplus=24.0,
                                comfort_savings_weight=0,
                                sched={str(h): "comfort" for h in range(24)})
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 22.0

    zone2, sensors2 = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                  surplus=True, temp_comfort=22.0, temp_surplus=24.0,
                                  comfort_savings_weight=0)
    d2 = mod._hp_zone_decision(sensors2, zone2, 0)
    assert d2["target"] == 24.0


def test_hp_zone_decision_emergency_when_sensor_missing_and_controlled(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, controlled_hvac=True,
                                temp_emergency=15.5)
    del sensors["temp_zone_0"]
    del sensors["temp_indoor"]
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 15.5
    assert "Emergency" in d["reason"]
    assert d["skip"] is False


def test_hp_zone_decision_emergency_inert_for_non_controlled_zone(mod, monkeypatch):
    """Without controlled_hvac, a missing sensor must keep the pre-feature
    behaviour (fall back to the 20.0 default inside the normal branches),
    not silently start emitting emergency targets for plain zones."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=False, temp_emergency=15.5)
    del sensors["temp_zone_0"]
    del sensors["temp_indoor"]
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 18.0
    assert "Emergency" not in d["reason"]


def test_hp_zone_decision_summer_controlled_thermal_override_uses_comfort_cool(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=True, controlled_hvac=True,
                                temp_comfort_cool=24.0)
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                         {"hvac_summer_override_c": 29.0}.get(k, d))
    sensors["temp_zone_0"] = 30.0
    sensors["temp_outdoor"] = 30.0  # not cooler than indoor → no free-cooling gate
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 24.0
    assert d["entity"] == "number.cool_setpoint"


def test_hp_zone_decision_summer_controlled_surplus_uses_surplus_cool(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=True, surplus=True,
                                controlled_hvac=True, temp_surplus_cool=21.0)
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                         {"hvac_summer_override_c": 29.0,
                          "cooling_outdoor_max_c": 22.0}.get(k, d))
    sensors["temp_outdoor"] = 28.0  # too warm outside for the free-cooling gate
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 21.0


def test_hp_zone_decision_summer_controlled_weight_blends_eco_tier(mod, monkeypatch):
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=True, controlled_hvac=True,
                                temp_comfort_cool=24.0, temp_min_cool=27.0,
                                comfort_savings_weight=0)
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                         {"hvac_summer_override_c": 29.0,
                          "cooling_outdoor_max_c": 22.0}.get(k, d))
    sensors["temp_outdoor"] = 28.0
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["target"] == 27.0  # weight=0 → pure eco (warmest)
    assert d["skip"] is False


def test_hp_zone_decision_summer_non_controlled_stays_inactive(mod, monkeypatch):
    """Backward compat: without controlled_hvac, the "no surplus, below
    override" case is still a full skip — no new eco-cooling tier appears
    for plain zones."""
    zone, sensors = _hvac_zone(mod, monkeypatch, summer=True)
    monkeypatch.setattr(mod, "cfg", lambda k, d=None:
                         {"hvac_summer_override_c": 29.0,
                          "cooling_outdoor_max_c": 22.0}.get(k, d))
    sensors["temp_outdoor"] = 28.0
    d = mod._hp_zone_decision(sensors, zone, 0)
    assert d["skip"] is True
