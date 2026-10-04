# ChargeWise

**Open-source EV charging cost & savings tracker** — see exactly what it costs to run your Tesla on electricity, and how much you're saving versus petrol, across the whole life of ownership.

> **Live in production since 11 July 2026.** Lifetime figures (Feb 2022 → 4 Oct 2026):
>
> | Sessions | Energy | Miles | Electricity | Petrol equivalent | **Saved** |
> |---|---|---|---|---|---|
> | 4,609 | 28,616 kWh | 80,950 | £3,849 | £18,338 | **£14,489** |
>
> That's **4.75 p/mile** electric versus **22.65 p/mile** petrol.
>
> Every home charge is priced from Octopus's published half-hourly rates for the tariff in force at the time (checked against the published records on every half-hour since Feb 2022). The figures are **not yet reconciled against a bill** and carry known limits — see [`docs/PROJECT-STATUS.md`](docs/PROJECT-STATUS.md). The figures published here before October 2026 priced four years of home charging at the wrong tariff and understated the saving; the incident is written up in the same document.

## What it does

ChargeWise combines three data sources you already have:

- **TeslaFi** — charge sessions, energy, home/away location, mileage (via the history API; dates are UTC, results arrive non-chronologically and can be partial under throttling — the adapter sorts, checksums the result count, and backs off on rate limits).
- **Octopus Energy** (Intelligent Octopus Go) — your real rates (the API returns pence; ChargeWise converts to GBP) and smart-dispatch slots, so home charging is costed *accurately*, not estimated.
- **GOV.UK weekly road fuel prices** — to value the equivalent petrol journey over the same period.

### Why it's accurate

Intelligent Octopus Go pricing is dynamic: the cheap rate applies to your whole home overnight (23:30–05:30), but in extra daytime slots only while the car is actively charging. ChargeWise reconciles each charge against the actual rates **and** the smart-dispatch slots in force at the time. See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — including its honest accuracy caveats (the dispatch feed only covers a recent window, so older daytime smart-charges are conservatively costed at peak).

## Architecture (v1, as deployed)

Two containers on an always-on box (a NAS in the reference setup), consumed by a Home Assistant dashboard:

```
┌─ NAS (Docker, docker-compose.nas.yml) ─────────────┐
│  chargewise-api        FastAPI on port 8000        │
│  chargewise-scheduler  daily ingestion loop        │
│                        (backend/scheduler.sh)      │
└──────────────────────────┬─────────────────────────┘
                           │ REST
              Home Assistant dashboard (ha/)
```

- **API endpoints:** `/health`, `/api/summary/lifetime`, `/api/status` (per-source last-sync + data freshness), `/api/charges`, `/api/settings`.
- **Scheduler:** re-runs the pipeline daily with a 35-day rolling re-cost window (re-runs are idempotent and refresh cost fields).
- **Deployment:** staged by [`scripts/deploy-nas.py`](scripts/deploy-nas.py) (streams files over SSH exec channels — no SFTP needed; the target comes from `NAS_*` environment variables or `local/deploy.env`) and run with [`docker-compose.nas.yml`](docker-compose.nas.yml).
- **Consumer:** the Home Assistant package and dashboard in [`ha/`](ha/README.md) — 20 REST entities covering the lifetime summary, API health, per-source sync status and errors, plus an alert when a sync fails or the API is unreachable.

**Honesty corner:** there is no web frontend yet. The Next.js standalone dashboard (and OAuth, and Azure deployment) are deliberately parked — the LAN-first v1 with the HA dashboard shipped instead. The root `docker-compose.yml` still declares a `frontend` service for that future work; for anything real today, use the `backend` service or `docker-compose.nas.yml`.

## Quick start

```bash
git clone https://github.com/AlistairWDoran/chargewise
cd chargewise

# 1. Run the backend tests (no credentials needed)
cd backend && python -m pytest && cd ..

# 2. Configure credentials
cp .env.example .env   # set TESLAFI_TOKEN, OCTOPUS_API_KEY, OCTOPUS_ACCOUNT_NUMBER
                       # (and, if you like, VEHICLE_MAP to give your cars names)

# 3. Run the API
docker compose up --build backend    # http://localhost:8000/health

# 4. Ingest your data (backfill, then let a scheduler keep it fresh)
docker compose run --rm backend python -m chargewise.ingest.pipeline --teslafi
```

Then point the Home Assistant package at your API — see [`ha/README.md`](ha/README.md).

For a 24/7 deployment on a NAS, use `docker-compose.nas.yml` (API + daily scheduler).

## Per-installation configuration

Installation-specific settings are read from two git-ignored places: `.env` (API keys, account number, vehicle names via `VEHICLE_MAP`) and the `local/` folder (your environment notes and the deploy target). Examples, tests and documents use placeholders such as `<chargewise-host>`, `192.0.2.x` and `TESTVIN0000000001`. A real-bill fixture written by `scripts/export-golden-fixture.py` is git-ignored as well, and the reconciliation test runs it wherever the file is. See [`local.example/README.md`](local.example/README.md).

## Project layout

```
backend/    FastAPI core, cost engine (pure & tested), ingestion, storage
ha/         Home Assistant package + Lovelace dashboard (reads the API)
scripts/    NAS deployment (SSH streaming) and firewall helpers
docs/       PRD, delivery plan, methodology, project status
local.example/  the convention for per-installation notes and the deploy target (local/)
```

## Develop

```bash
make test       # backend test suite
make lint       # ruff
make typecheck  # mypy
```

## Learn more

- [`docs/PROJECT-STATUS.md`](docs/PROJECT-STATUS.md) — current state, the 2026 incident, operations, hard-won lessons
- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — how costs and savings are calculated
- [`docs/PRD.md`](docs/PRD.md) and [`docs/DELIVERY-PLAN.md`](docs/DELIVERY-PLAN.md) — product intent and plan
- [`ha/README.md`](ha/README.md) — Home Assistant setup

## Licence

[MIT](LICENSE) © 2026 Alistair Doran. UK fuel data © Crown copyright, used under the Open Government Licence.
