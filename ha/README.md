# ChargeWise — Home Assistant dashboard

A Home Assistant front-end for ChargeWise that reads the ChargeWise API over
REST and shows lifetime cost, savings vs petrol, and cost-per-mile.

```
ha/
├── packages/chargewise.yaml     REST sensors (lifetime summary + API health)
├── dashboards/chargewise.yaml   Lovelace dashboard (sections view)
├── secrets.example.yaml         URLs / token to copy into your secrets.yaml
└── README.md                    this file
```

## Prerequisites

- A running ChargeWise API reachable from Home Assistant (see the backend
  README — `uvicorn chargewise.api.app:create_app --factory`, or the container).
  For a LAN-only setup, run it with `AUTH_DISABLED=true` so no token is needed.
- The API populated with data via the ingestion pipeline
  (`python -m chargewise.ingest.pipeline …`).

## Setup

1. **Secrets.** Copy the entries from `secrets.example.yaml` into your
   `<config>/secrets.yaml` and set `chargewise_summary_url` /
   `chargewise_health_url` to your API host. Add `chargewise_auth` (and
   uncomment the `Authorization` header in `packages/chargewise.yaml`) only if
   the API has auth enabled.

2. **Sensors (package).** Enable packages once in `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

   then copy `packages/chargewise.yaml` to `<config>/packages/chargewise.yaml`.
   `rest:` is a startup integration, so **restart Home Assistant** (or reload
   the REST YAML) to load the sensors.

3. **Dashboard.** Either paste the `views:` block from
   `dashboards/chargewise.yaml` into a new dashboard's Raw configuration editor,
   or register it in YAML mode:

   ```yaml
   lovelace:
     dashboards:
       chargewise:
         mode: yaml
         filename: dashboards/chargewise.yaml
         title: ChargeWise
         icon: mdi:car-electric
   ```

## Entities created

| Entity | Meaning |
|--------|---------|
| `sensor.chargewise_lifetime_saving` | Lifetime saving vs petrol (£) |
| `sensor.chargewise_lifetime_cost` | Lifetime electricity spend (£) |
| `sensor.chargewise_petrol_equivalent_cost` | What petrol would have cost (£) |
| `sensor.chargewise_home_charging_cost` | Home charging cost (£) |
| `sensor.chargewise_away_charging_cost` | Public charging cost (£) |
| `sensor.chargewise_total_energy` | Total energy charged (kWh) |
| `sensor.chargewise_total_miles` | Total miles |
| `sensor.chargewise_electric_cost_per_mile` | Electric p/mile |
| `sensor.chargewise_petrol_cost_per_mile` | Petrol p/mile |
| `sensor.chargewise_session_count` | Number of charge sessions |
| `binary_sensor.chargewise_api` | API reachable (connectivity) |

## Notes

- Polling is gentle (summary every 30 min, health every 5 min) — the figures
  change slowly. If the API is unreachable the sensors report unavailable and
  `binary_sensor.chargewise_api` turns off.
- The "Saving trend" history graph fills in over time as Home Assistant records
  the saving sensor; it will be empty on first install.
