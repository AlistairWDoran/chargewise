# ChargeWise — Delivery Plan, Technical Specifications & Development Harness

**Companion to:** `PRD.md` (v0.1)
**Version:** 0.1 (Draft for review)
**Date:** 8 June 2026
**Profile context:** Personal
**Confirmed decisions:** Name **ChargeWise** · DB **SQLite** (abstracted) · Stack **FastAPI + React/Next.js** · Access **internet-reachable with login** · Hosting **Azure Container Apps** · Licence **MIT**

---

## 1. How to use this document

This turns the PRD into something buildable: the engineering standards, the architecture and specs in enough detail to implement, the development harness (the scaffolding, tooling and tests that keep quality high), and a phased plan with clear "done" gates. It is deliberately implementation-ready but not yet code. Build begins only after sign-off.

---

## 2. Engineering principles

- **The cost engine is pure and isolated.** Given sessions, rates and dispatches in, it returns costs out — no I/O, no network. This is what we test hardest and trust most.
- **Ingestion is idempotent and resumable.** Re-running an import or poll never duplicates or corrupts data.
- **Source of truth is explicit.** TeslaFi = energy + location + mileage. Octopus = price + dispatches. GOV.UK = fuel price. We never silently invent data; estimates are flagged.
- **Abstract the database.** Repository interfaces sit between logic and SQLite so Postgres is a swap, not a rewrite.
- **Same image everywhere.** Local Docker Compose and Azure run identical containers.
- **Document the maths.** Users must be able to read *how* a number was produced and trust it.
- **Secrets never touch git.** `.env` + secret store only; CI scans for leaks.

---

## 3. Architecture (detailed)

### 3.1 Components

| Component | Tech | Responsibility |
|---|---|---|
| **Ingestion workers** | Python | Pull/parse TeslaFi (CSV + live), Octopus (REST rates/consumption + GraphQL dispatches), GOV.UK fuel CSV; normalise into the data model |
| **Cost engine** | Python (pure) | Slot-level, dispatch-aware home costing; away costing; mileage; petrol-equivalent & savings |
| **Persistence** | SQLite via repository layer | Store raw + derived data; expose aggregates |
| **API** | FastAPI | Serve dashboard data + trigger ingest; auth-protected |
| **Standalone UI** | React / Next.js | Overview / History / Savings / Settings; clean, glanceable |
| **Auth** | FastAPI auth (pluggable) | Login for the internet-facing dashboard; HTTPS enforced |
| **HA bridge** | HA template sensors / REST sensor | Surface headline figures in Home Assistant |
| **Scheduler** | Container Apps job / cron | Live poll + weekly fuel refresh + nightly recompute |

### 3.2 Data flow

```
TeslaFi CSV ─┐
TeslaFi API ─┤
Octopus REST ┼─► Ingestion ─► raw tables ─► Cost engine ─► derived tables ─► API ─► UI
Octopus GQL ─┤                                   ▲                            └─► HA bridge
GOV.UK CSV ──┘                              (pure, tested)
```

### 3.3 Repository structure

```
chargewise/
├── README.md  LICENSE (MIT)  CONTRIBUTING.md  CODE_OF_CONDUCT.md  CHANGELOG.md
├── .github/workflows/   ci.yml  release.yml
├── docs/                PRD.md  DELIVERY-PLAN.md  METHODOLOGY.md  DEPLOY.md  SETUP-CREDENTIALS.md
├── docker-compose.yml   .env.example   Makefile
├── backend/
│   ├── chargewise/
│   │   ├── ingest/      teslafi_csv.py  teslafi_live.py  octopus_rest.py  octopus_graphql.py  fuel_prices.py
│   │   ├── engine/      cost.py  mileage.py  savings.py        # pure, no I/O
│   │   ├── store/       models.py  repositories.py  migrations/
│   │   ├── api/         app.py  routes/  auth.py  schemas.py
│   │   └── config.py
│   ├── tests/           unit/  integration/  fixtures/  golden/
│   └── pyproject.toml
├── frontend/            app/  components/  lib/  package.json   # Next.js
└── homeassistant/       packages/chargewise.yaml  dashboard-chargewise.yaml  README.md
```

---

## 4. Technical specifications

### 4.1 Data model (SQLite DDL — abbreviated)

```sql
CREATE TABLE vehicle (
  id INTEGER PRIMARY KEY, name TEXT, vin TEXT,
  acquired_date TEXT, disposed_date TEXT);

CREATE TABLE charge_session (
  id INTEGER PRIMARY KEY, vehicle_id INTEGER REFERENCES vehicle(id),
  start_utc TEXT, end_utc TEXT, location_type TEXT,        -- 'home' | 'away'
  energy_kwh REAL, odometer REAL, source TEXT,             -- 'teslafi_csv' | 'teslafi_live'
  raw_cost REAL, raw_cost_is_estimate INTEGER,
  UNIQUE(vehicle_id, start_utc, energy_kwh));              -- idempotency key

CREATE TABLE charge_slot (
  id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES charge_session(id),
  slot_start_utc TEXT, energy_kwh REAL, unit_rate REAL,
  rate_source TEXT, cost REAL);                            -- 'core'|'dispatch'|'standard'

CREATE TABLE tariff_agreement (product_code TEXT, tariff_code TEXT, valid_from TEXT, valid_to TEXT);
CREATE TABLE unit_rate (tariff_code TEXT, valid_from TEXT, valid_to TEXT, value_inc_vat REAL, value_exc_vat REAL);
CREATE TABLE dispatch_slot (start_utc TEXT, end_utc TEXT, source TEXT);   -- 'completed'|'planned'
CREATE TABLE consumption_hh (meter TEXT, interval_start_utc TEXT, consumption_kwh REAL);
CREATE TABLE fuel_price_week (week_start TEXT, fuel_type TEXT, pence_per_litre REAL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);  -- mpg, fuel_type, away_rate, tariff, etc.
```

All timestamps stored UTC (ISO 8601); presentation converts to Europe/London.

### 4.2 Cost engine (the core algorithm)

```
cost_home_session(session, rates, dispatches):
    slots = split_into_half_hours(session.start, session.end)
    energy_per_slot = apportion(session.energy_kwh, slots)     # even by duration; cross-check vs consumption_hh
    for slot in slots:
        if in_core_window(slot):           rate, src = offpeak_rate(slot, rates), 'core'
        elif covered_by_dispatch(slot, dispatches): rate, src = offpeak_rate(slot, rates), 'dispatch'
        else:                              rate, src = standard_rate(slot, rates), 'standard'
        slot.cost = round_octopus(energy_per_slot[slot] * rate)
    return sum(slot.cost for slot in slots)
```

Rules: rates resolved from `tariff_agreement` + `unit_rate` history valid at the slot time; VAT-inclusive for display, VAT-exclusive retained for bill reconciliation; Octopus "unbiased" rounding applied; core window 23:30–05:30 local (configurable per tariff). Away sessions use `raw_cost`, falling back to `energy_kwh × away_rate` flagged as estimate. Savings: `petrol_cost = miles/mpg × 4.546 × pence_per_litre/100`; `saving = petrol_cost − electric_cost`.

### 4.3 Ingestion adapters

- **TeslaFi history API (primary)** — `GET history.php?command=charges` (and `command=drives`) with `Authorization: Bearer <token>` and `dateFrom`/`dateTo`. Backfill in date chunks across Feb 2022→present; poll recent window on schedule for ongoing. Map JSON to `charge_session` (+ odometer/mileage); idempotent upsert on the unique key; attribute to `vehicle`. Beta endpoint — pin to a known field set, validate responses, fail gracefully. Prefer actual Supercharger invoice cost where present.
- **TeslaFi CSV (fallback)** — parse monthly exports for any period/car the API doesn't cover; same mapping and idempotent upsert.
- *Open items confirmed via `docs/TESLAFI-SUPPORT-EMAIL.md`: exact field names, multi-vehicle behaviour, pagination, rate limits, invoice-cost field, beta stability.*
- **Octopus REST** — `/accounts/{acct}/` for tariff agreement history; `/products/.../standard-unit-rates/` for rates over range; `/electricity-meter-points/.../consumption/` for HH consumption (reconciliation). Basic auth, API key as username.
- **Octopus GraphQL** — completed/planned dispatches for IOG. Token auth.
- **GOV.UK fuel** — download weekly CSV (2018→present, plus 2003–2017 for early history); upsert by week; weekly refresh.

### 4.4 API (FastAPI) — indicative endpoints

`GET /api/summary/lifetime` · `GET /api/summary/period?from&to&group=day|week|month` · `GET /api/charges?from&to&location=` · `GET /api/savings?from&to` · `GET /api/settings` / `PUT /api/settings` · `POST /api/ingest/{source}` · `POST /api/recompute`. All behind auth except health check. JSON schemas defined in `schemas.py`; OpenAPI auto-generated.

### 4.5 Standalone UI (Next.js)

- **Overview** — lifetime cost, lifetime saving vs petrol, this-month cost, last-charge cost, p/mile electric vs petrol.
- **History** — filterable charge log (date range, home/away), per-session cost + rate source.
- **Savings** — cumulative saving and monthly cost-vs-petrol charts.
- **Settings** — credentials, mpg, fuel type, away-rate, tariff, ingest triggers.
- Design tokens (colour/type/spacing) fixed up front per the PRD's UX principles; estimates visibly labelled.

### 4.6 Home Assistant bridge

Per HA best practices (no `.storage` edits; entity_id based; correct helper/template choice): a `packages/chargewise.yaml` exposing headline values either (a) computed from existing Octopus + Tesla entities, or (b) via a REST sensor reading the ChargeWise API (recommended single-source-of-truth — open question §17.5 in PRD). A Lovelace dashboard YAML using Mushroom/standard cards.

### 4.7 Auth

Internet-facing, so: HTTPS enforced, login required, sessions/tokens with sane expiry, password hashing, rate-limited login. Pluggable so self-hosters can use an external provider (e.g. OAuth/Azure AD); a simple built-in login ships by default.

---

## 5. Development harness

The harness is what makes this a *high-quality* product rather than a script — it must exist before feature work.

- **Scaffolding & reproducibility:** `docker-compose.yml` (backend + frontend + volume) and `.env.example`; a `Makefile` with `make dev / test / lint / typecheck / fmt / ingest-sample`.
- **Sample/mock data:** anonymised TeslaFi CSV fixtures, canned Octopus rate/dispatch JSON, a slice of GOV.UK fuel CSV — so the whole stack runs with zero real credentials for development and CI.
- **Test strategy (pyramid):**
  - *Unit* — the cost engine is the priority: slot splitting, core-window logic, dispatch coverage, tariff-change boundaries, BST/GMT changeover, rounding. Pure functions, fast, high coverage.
  - *Integration* — adapters parse real-shaped fixtures into the model; repository layer round-trips; API endpoints return correct shapes behind auth.
  - *Golden / reconciliation test* — **the key verification**: a known month of charges priced by the engine reconciles to within ~1–2% of an actual Octopus bill figure. Stored as a golden fixture and asserted in CI.
  - *Frontend* — component tests + a smoke e2e (Playwright) on the main views.
- **Quality tooling:** `ruff` + `black` (lint/format), `mypy` (types), `pytest` + coverage gate; `eslint`/`prettier`/`tsc` for frontend; `pre-commit` hooks running all of the above.
- **CI/CD (GitHub Actions):** `ci.yml` → lint, typecheck, tests (with coverage threshold), build Docker images, run secret-scanning (e.g. gitleaks); `release.yml` → semver tag → build/push image + changelog. Branch protection on `main`.
- **Local dev loop:** clone → `cp .env.example .env` → `make dev` → app up with sample data → `make test` green.

---

## 6. Phased delivery plan

Each phase ends at a demoable, tested increment with an explicit Definition of Done (DoD).

**Phase 0 — Harness & scaffolding.** Repo, licence, docs skeleton, Docker Compose, Makefile, CI, pre-commit, sample fixtures, empty module structure. *DoD: `make dev` and `make test` run green on an empty skeleton; CI passes.*

**Phase 1 — Data model & cost engine.** Schema + repositories; pure cost engine with full unit tests incl. IOG dispatch logic and the golden reconciliation fixture. *DoD: engine prices sample sessions correctly; golden test within tolerance; 90%+ engine coverage.*

**Phase 2 — Ingestion.** TeslaFi CSV import (historical backfill), Octopus REST rates + GraphQL dispatches, GOV.UK fuel import; idempotency tests. *DoD: real history loads end-to-end into the DB; re-running imports causes no duplication.*

**Phase 3 — API + live polling.** FastAPI endpoints, auth, scheduled live TeslaFi poll + weekly fuel refresh + nightly recompute. *DoD: authenticated API returns correct summaries; scheduler runs; OpenAPI published.*

**Phase 4 — Standalone dashboard.** Next.js Overview/History/Savings/Settings on the design tokens; estimate labelling; responsive. *DoD: clean, glanceable UI passes smoke e2e; headline numbers match API.*

**Phase 5 — Home Assistant dashboard.** HA package + Lovelace dashboard surfacing headline figures (via service API, recommended). *DoD: cards render in Alistair's HA with correct live values; follows HA best practices.*

**Phase 6 — Azure deployment.** Container Apps + Azure Files volume + secrets; HTTPS + login live; deploy docs. *DoD: instance reachable behind login at near-zero idle cost; `DEPLOY.md` reproducible.*

**Phase 7 — Open-source hardening & release.** README with screenshots, METHODOLOGY.md, SETUP-CREDENTIALS.md, contribution files, v1.0.0 tagged. *DoD: a third party can deploy from docs unaided; secret-scan clean; release published.*

---

## 7. Quality gates & verification

- Every phase: lint + types + tests + secret-scan green in CI before merge.
- **Accuracy gate (the headline verification):** the golden reconciliation test must hold (≈1–2% vs a real Octopus bill month) — this is the project's trust anchor and is re-asserted in CI.
- Completeness check: lifetime total covers Feb 2022→present with gaps explicitly reported.
- UX check: headline saving + monthly cost readable at a glance on phone and desktop (screenshot review).
- Deploy check: clean-room deploy from `DEPLOY.md` succeeds.

---

## 8. Documentation plan

`README.md` (what/why + screenshots), `docs/METHODOLOGY.md` (how every number is calculated — essential for trust), `docs/SETUP-CREDENTIALS.md` (getting TeslaFi token + Octopus API key + account number, exporting CSVs), `docs/DEPLOY.md` (local Docker + Azure), `CONTRIBUTING.md`, `CHANGELOG.md`. GOV.UK data attributed under OGL.

---

## 9. Delivery risks

| Risk | Mitigation |
|---|---|
| Cost-engine correctness | Golden reconciliation test gating CI; methodology documented and reviewed |
| Real credentials needed to validate | Build on fixtures; validate against Alistair's real data at Phase 2/7 |
| Internet-facing auth adds scope | Keep built-in login minimal but correct; pluggable for stronger providers; HTTPS enforced |
| Per-slot energy apportionment error | Cross-check vs Octopus consumption; document; keep within accuracy gate |
| Scope creep to TCO | Held to PRD non-goals; extensible model leaves the door open |

---

## 10. Immediate next actions (on approval)

1. Confirm remaining PRD open questions (§17.5–7): HA-data approach, TeslaFi history availability, auth provider.
2. Lock the visual design tokens (a short style reference) before front-end work.
3. Stand up **Phase 0** (repo + harness + CI) — the foundation everything else builds on.
4. Export TeslaFi history CSVs (Feb 2022→present) and obtain Octopus API key + account number, ready for Phase 2 validation.

---

*End of Delivery Plan v0.1 — for review alongside PRD v0.1.*
