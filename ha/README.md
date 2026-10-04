# ChargeWise — Home Assistant dashboard

A Home Assistant front-end for ChargeWise that reads the ChargeWise API over
REST and shows lifetime cost, savings vs petrol, cost-per-mile, and per-source
data freshness. It also tells you when the daily ingestion is broken.

```
ha/
├── packages/chargewise.yaml     REST sensors (lifetime summary, API health, sync status) + alert automation
├── dashboards/chargewise.yaml   Lovelace dashboard (sections view)
├── secrets.example.yaml         URLs / token to copy into your secrets.yaml
└── README.md                    this file
```

## Prerequisites

- A running ChargeWise API reachable from Home Assistant (see the root
  README — `uvicorn chargewise.api.app:create_app --factory`, or the container).
  For a LAN-only setup, run it with `AUTH_DISABLED=true` so no token is needed.
- The API populated with data via the ingestion pipeline
  (`python -m chargewise.ingest.pipeline …`).
- A ChargeWise API recent enough to send `healthy` and `last_error` in
  `/api/status`. Against an older API the package still loads, but it reports a
  sync problem (see "Sync problems and alerting").
- Home Assistant 2024.10 or later: the alert automation uses the
  `triggers:` / `actions:` syntax.

## Setup

1. **Secrets.** Copy the entries from `secrets.example.yaml` into your
   `<config>/secrets.yaml` and set `chargewise_summary_url`,
   `chargewise_health_url` and `chargewise_status_url` to your API host
   (they point at `/api/summary/lifetime`, `/health` and `/api/status`
   respectively). Add `chargewise_auth` (and uncomment the `Authorization`
   header in `packages/chargewise.yaml`) only if the API has auth enabled.

2. **Sensors (package).** Enable packages once in `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

   then copy `packages/chargewise.yaml` to `<config>/packages/chargewise.yaml`.
   The file holds the REST sensors and one automation. `rest:` is a startup
   integration, so **restart Home Assistant** to load them. A YAML reload of REST re-reads the sensor definitions but does
   **not** re-resolve `!secret` values — so after adding or changing any
   `chargewise_*` secret (e.g. adding `chargewise_status_url`), a full restart
   is required, not a reload. (Hard-won lesson.)

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

### Updating an existing install

Deployment is manual; nothing in this repo pushes to Home Assistant.

1. Make sure the ChargeWise API has been updated first. Open your
   `chargewise_status_url` in a browser: the answer should contain a
   `healthy` key.
2. Keep a copy of the current `<config>/packages/chargewise.yaml`, then
   replace it with the file from this repo. No new secrets are needed — the
   new entities read the existing `chargewise_status_url`.
3. Run a configuration check, then call the `rest.reload` and
   `automation.reload` services (Developer tools → YAML offers the check and
   both reloads). That is enough; no restart is needed unless a secret was
   added or changed.
4. Update the dashboard: open it, Edit → Raw configuration editor, and paste
   the `views:` block from `dashboards/chargewise.yaml` (or, in YAML mode,
   replace the file). The sync-problem badge, the "Daily sync" tile and the
   "Last run, per feed" card need the sensors from step 2.
5. Check that the four entities under "Sync problems and alerting" exist, that
   `automation.chargewise_sync_problem_notification` is on, and that
   `binary_sensor.chargewise_sync_problem` is off once the API reports
   `healthy: true`.

If you edit files through the File Editor add-on, note that its base path is
`/homeassistant`, not `/config`: the package is
`/homeassistant/packages/chargewise.yaml` there.

### Reference install (4 October 2026)

The package and dashboard in this folder are installed on the project's own
Home Assistant:

- **Package:** installed unchanged, with a copy of the previous file kept. The
  configuration check passed, and `rest.reload` and `automation.reload` brought
  it into service without a restart. Result: 20 ChargeWise REST entities plus
  `automation.chargewise_sync_problem_notification`, which is on;
  `binary_sensor.chargewise_sync_problem` is off and the three last-error
  sensors read `OK`.
- **Dashboard:** the live dashboard is kept in storage mode (edited in the
  UI), and it had drifted from `dashboards/chargewise.yaml`. It now has the
  same cards as the file, but in a different order within the "Status & data
  freshness" section.

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
| `sensor.chargewise_teslafi_last_sync` | TeslaFi: last successful sync (timestamp) |
| `sensor.chargewise_octopus_last_sync` | Octopus: last successful sync (timestamp) |
| `sensor.chargewise_fuel_prices_last_sync` | GOV.UK fuel prices: last successful sync (timestamp) |
| `sensor.chargewise_latest_charge` | Most recent charge session in the data (timestamp) |
| `sensor.chargewise_fuel_prices_week` | Week of the latest fuel-price data |
| `binary_sensor.chargewise_sync_problem` | On when the daily ingestion has a problem (problem) |
| `sensor.chargewise_teslafi_last_error` | TeslaFi: why the last run failed, or `OK` |
| `sensor.chargewise_octopus_last_error` | Octopus: why the last run failed, or `OK` |
| `sensor.chargewise_fuel_prices_last_error` | GOV.UK fuel prices: why the last run failed, or `OK` |

## Status & data freshness

The third REST resource (`chargewise_status_url` → `/api/status`) feeds five
sensors that answer "is the data actually up to date?" — one last-sync
timestamp per source (TeslaFi, Octopus, fuel prices), plus **Latest Charge**
and **Fuel Prices Week**. (The same resource feeds the four entities in the
next section.) Sync success and data freshness are deliberately
distinct signals: TeslaFi can sync cleanly while its own logging is stalled,
which is exactly what a stale **Latest Charge** reveals even when the last-sync
sensors look healthy.

The dashboard's **"Status & data freshness"** section surfaces these alongside
the API connectivity tile, so a stalled feed is visible at a glance instead of
silently freezing the headline figures.

## Sync problems and alerting

Four entities and one automation answer "did the daily run work?".

**`binary_sensor.chargewise_sync_problem`** is off only when `/api/status`
answers `healthy: true`. The API sets `healthy` to false when any feed's last
run failed, or when any feed has not succeeded for 36 hours — which also
catches the daily job not running at all. The sensor fails closed: a missing
`healthy` field or an answer that is not the expected JSON also turns it on.
If the API cannot be reached, the sensor becomes unavailable.

**The three last-error sensors** each show one of:

| State | Meaning |
|-------|---------|
| `OK` | The API reports no error for that feed. |
| the error text | Why that feed's most recent run failed (under 255 characters, secrets masked by the API). |
| `Not reported` | The answer had no `last_error` field for that feed: the API is an older version, or its answer was not understood. |

`OK` does not by itself mean the feed is current. A feed can show `OK` with a
last success days old, if the run stopped at an earlier feed or did not run;
the problem sensor covers that case through the 36-hour rule.

**Against an older API** (one that predates `healthy` and `last_error`), the
problem sensor is on, all three error sensors read `Not reported`, and the
notification below is raised after 30 minutes. Update the API first.

**The automation** ("ChargeWise: sync problem notification") keeps one
persistent notification in step with the sensors:

| Condition | Result |
|-----------|--------|
| Sync problem on for 30 minutes | Notification "ChargeWise sync problem", listing each feed's last error and last success |
| API unreachable for 30 minutes (`binary_sensor.chargewise_api` off, or either sensor unavailable or unknown) | Notification "ChargeWise API unreachable" |
| Sync problem off and API on | Notification dismissed |

- It checks on every change of either binary sensor and again every
  5 minutes. With the 15-minute poll of `/api/status`, expect the
  notification 30 to 50 minutes after a failed run.
- The 5-minute check exists because a restart of Home Assistant clears
  persistent notifications, and a problem that began before the restart
  produces no state change to trigger on. After a restart the notification is
  raised again once the 30-minute condition holds again.
- For the same reason, a notification dismissed by hand comes back within
  5 minutes while the problem lasts. Fix the problem rather than the
  notification.
- A problem that comes and goes — never staying on for 30 minutes at a
  stretch — does not raise the notification.
- The notification appears only in Home Assistant. To get it on a phone, swap
  the `persistent_notification.create` action for a `notify.*` service; note
  that the action runs every 5 minutes while the problem lasts.
- The automation does not look at the age of **Latest Charge**. A feed that
  syncs cleanly but brings no new charges raises nothing.

**What has been tested, and what has not.** The alert logic was exercised in a
real Home Assistant core (2026.2.3) under a simulated clock: it notifies after
30 minutes of a sync problem or of the API being unreachable, dismisses on
recovery, and re-raises after a restart. On the live instance the automation
is installed, enabled and healthy, and a manual trigger ran cleanly — but it
has **not yet been seen to fire** there. A live drill is an open item
(`docs/BACKLOG.md`).

## Notes

- Polling is gentle (summary every 30 min, sync status every 15 min, health
  every 5 min) — the figures change slowly. If the API is unreachable the
  sensors report unavailable and `binary_sensor.chargewise_api` turns off.
- The "Saving trend" history graph fills in over time as Home Assistant records
  the saving sensor; it will be empty on first install.
