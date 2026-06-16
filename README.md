# ChargeWise

**Open-source EV charging cost & savings tracker** — see exactly what it costs to run your Tesla on electricity, and how much you're saving versus petrol, both right now and across the whole life of ownership.

ChargeWise combines three data sources you already have:

- **TeslaFi** — charge sessions, energy, home/away location, mileage.
- **Octopus Energy** (Intelligent Octopus Go) — your real half-hourly rates and smart-charge dispatch slots, so home charging is costed *accurately*, not estimated.
- **GOV.UK weekly road fuel prices** — to value the equivalent petrol journey over the same period.

Two dashboards from one core: a **Home Assistant** dashboard and a **standalone web app** (works without HA).

> Status: early development. See [`docs/PRD.md`](docs/PRD.md) and [`docs/DELIVERY-PLAN.md`](docs/DELIVERY-PLAN.md).

## Why it's accurate

Intelligent Octopus Go pricing is dynamic: the cheap rate applies to your whole home overnight (23:30–05:30), but in extra daytime slots only while the car is actively charging. ChargeWise reconciles each charge against the actual rates **and** the smart-dispatch slots in force at the time. See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md).

## Project layout

```
backend/      FastAPI core, cost engine (pure & tested), ingestion, storage
frontend/     Next.js standalone dashboard (warm, themeable, light/dark)
homeassistant/ HA package + Lovelace dashboard (reads the ChargeWise API)
docs/         PRD, delivery plan, methodology, support email
```

## Develop

```bash
make dev      # run backend + frontend with sample data (no real credentials needed)
make test     # run the test suite
make lint     # ruff + mypy
```

## Licence

[MIT](LICENSE) © 2026 Alistair Doran. UK fuel data © Crown copyright, used under the Open Government Licence.
