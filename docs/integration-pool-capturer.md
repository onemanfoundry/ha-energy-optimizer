# Integration: Idegis/AstralPool pool capturer

The pool chlorinator capturer add-on
([`ha-idegis-astralpool`](https://github.com/hirofairlane/ha-idegis-astralpool))
estimates the electricity cost of the pool pumps, splitting consumption into
**grid** (priced by ToU period) vs **solar** (PV surplus, free). It currently
duplicates this project's tariff geometry because it cannot read our private
`/data/tariff.json` at runtime (we are ingress-only). This note defines the
contract to converge the two add-ons. Full spec lives in the capturer repo:
`docs/16-energy-cost-integration.md`.

## What we (Energy Optimizer) should PUBLISH

Expose canonical Home Assistant sensors (MQTT discovery or Supervisor API) so
the capturer — and any other consumer — can read pricing/flow through HA instead
of duplicating config. Single source of truth = this project.

| Sensor | Unit | Notes |
|---|---|---|
| `sensor.energy_optimizer_import_price` | €/kWh | Current import price; attribute `period: peak\|mid\|valley`. Source: `current_tariff()` (`energy_optimizer.py`). |
| `sensor.energy_optimizer_export_price` | €/kWh | Current feed-in price (`prices.export`). |
| `sensor.energy_optimizer_grid_power` | W | Signed grid power (re-expose the user's grid sensor) so consumers needn't know the wired entity id. Source: `read_sensors()`. |
| `sensor.energy_optimizer_power_source` | — | Convenience enum `solar\|grid\|mixed` from the current balance. |

These already exist internally and are served at `GET /api/status` and
`GET /api/tariff`; publishing them as HA sensors just makes them reusable.

## What we can CONSUME from the capturer

To fold pool-pump consumption into our savings/optimisation accounting, read
`GET /api/idegis/pumps` from the capturer (port 8765 / ingress). Relevant fields
per channel (`pump`, `cleaner`):

- `cost.{24h,7d,30d}`: `grid_kwh`, `solar_kwh`, `grid_eur`,
  `solar_export_value_eur`, `solar_pct`, `by_period_eur{peak,mid,valley}`
- `source_now`: `solar | grid | idle`
- `now_w`, `kwh_30d`, `eur_30d`

The pool pump is a deferrable load (turnover has slack); knowing its ToU-split
cost lets the optimiser schedule filtration into valley / solar-surplus windows.
