# ChargeWise

**Open-source EV charging cost & savings tracker** — see exactly what it costs to run your Tesla on electricity, and how much you're saving versus petrol, across the whole life of ownership.

> **Live in production since 11 July 2026.** Verified lifetime figures (Feb 2022 → 12 Jul 2026):
>
> | Sessions | Energy | Miles | Electricity | Petrol equivalent | **Saved** |
> |---|---|---|---|---|---|
> | 4,403 | 26,735 kWh | 76,245 | £9,027 | £17,178 | **£8,152** |
>
> That's **11.84 p/mile** electric versus **22.53 p/mile** petrol.

## What it does

ChargeWise combines three data sources you already have:

- **TeslaFi** — charge sessions, energy, home/away location, mileage (via the history API; dates are UTC, results arrive non-chronologically and can be partial under throttling — the adapter sorts, checksums the result count, and backs off on rate limits).
- **Octopus Energy** (Intelligent Octopus Go) — your real rates (the API returns pence; ChargeWise converts to GBP) and smart-dispatch slots, so home charging is costed *accurately*, not estimated.
- **GOV.UK weekly road fuel prices** — to value the equivalent petrol journey over the same period.

### Why it's accurate

Intelligent Octopus Go pricing is dynamic: the cheap rate applies to your whole home overnight (23:30–05:30), but in extra daytime slots only while the car is actively charging. ChargeWise reconciles each charge against the actual rates **and** the smart-dispatch slots in force at the time. See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — including its honest accuracy caveats (the dispatch feed only covers a recent window, so older daytime smart-charges are conservatively costed at peak).

## Architecture (v1, as deployed)

Two containers on an always-on box (a Synology NAS in the reference setup), consumed by a Home Assistant dashboard:

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
- **Deployment:** staged by [`scripts/deploy-nas.py`](scripts/deploy-nas.py) (streams files over SSH exec channels — no SFTP needed) and run with [`docker-compose.nas.yml`](docker-compose.nas.yml).
- **Consumer:** the Home Assistant package and dashboard in [`ha/`](ha/README.md) — 16 REST sensors covering the lifetime summary, API health, and per-source data freshness.

**Honesty corner:** there is no web frontend yet. The Next.js standalone dashboard (and OAuth, and Azure deployment) are deliberately parked — the LAN-first v1 with the HA dashboard shipped instead. The root `docker-compose.yml` still declares a `frontend` service for that future work; for anything real today, use the `backend` service or `docker-compose.nas.yml`.

## Quick start

```bash
git clone https://github.com/AlistairWDoran/chargewise
cd chargewise

# 1. Run the backend tests (no credentials needed)
cd backend && python -m pytest && cd ..

# 2. Configure credentials
cp .env.example .env   # set TESLAFI_TOKEN, OCTOPUS_API_KEY, OCTOPUS_ACCOUNT_NUMBER

# 3. Run the API
docker compose up --build backend    # http://localhost:8000/health

# 4. Ingest your data (backfill, then let a scheduler keep it fresh)
docker compose run --rm backend python -m chargewise.ingest.pipeline --teslafi
```

Then point the Home Assistant package at your API — see [`ha/README.md`](ha/README.md).

For a 24/7 deployment on a Synology NAS, use `docker-compose.nas.yml` (API + daily scheduler).

## Project layout

```
backend/    FastAPI core, cost engine (pure & tested), ingestion, storage
ha/         Home Assistant package + Lovelace dashboard (reads the API)
scripts/    NAS deployment (SSH streaming) and firewall helpers
docs/       PRD, delivery plan, methodology, project status
```

## Develop

```bash
make test       # backend test suite
make lint       # ruff
make typecheck  # mypy
```

## Learn more

- [`docs/PROJECT-STATUS.md`](docs/PROJECT-STATUS.md) — current state, environment, hard-won lessons
- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — how costs and savings are calculated
- [`docs/PRD.md`](docs/PRD.md) and [`docs/DELIVERY-PLAN.md`](docs/DELIVERY-PLAN.md) — product intent and plan
- [`ha/README.md`](ha/README.md) — Home Assistant setup

## Licence

[MIT](LICENSE) © 2026 Alistair Doran. UK fuel data © Crown copyright, used under the Open Government Licence.
