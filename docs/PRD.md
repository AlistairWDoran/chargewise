# Product Requirements Document — ChargeWise (EV Charging Cost Tracker)

**Product name:** ChargeWise
**Author:** Alistair Doran
**Profile context:** Personal
**Version:** 0.1 (Draft for review)
**Date:** 8 June 2026
**Status:** Draft — pending sign-off before delivery planning
**Licence (intended):** MIT · **Repo host:** GitHub (public)

---

## Status (12 Jul 2026) — v1 SHIPPED

> **This PRD is preserved as written (8 Jun 2026). v1 went LIVE on 11–12 July 2026 with a deliberately re-scoped deployment.** See `PROJECT-STATUS.md` for the authoritative current state.
>
> **What shipped vs this PRD's scope:**
> - **Shipped (LAN-only v1):** Docker on the Synology NAS — FastAPI `chargewise-api` plus a daily incremental scheduler. The **Home Assistant dashboard is the only front-end**, reading the ChargeWise API via 16 REST sensors. Full backfill to Feb 2022 via the TeslaFi history API (no CSV import needed), Octopus REST/GraphQL rate and dispatch ingestion, GOV.UK fuel prices, dispatch-aware cost engine per §8.
> - **Parked, not abandoned:** the standalone **Next.js web dashboard**, **OAuth/Entra login** and **Azure hosting** (§10, §12, decisions 4, 6, 10, 12). v1 was re-scoped to LAN-only after a mentor review that prioritised shipped value over infrastructure. Revisit only if the internet-facing product is still wanted.
>
> **Verified headline figures (Feb 2022 → 12 Jul 2026, agent-verified):** 4,403 sessions · 26,735 kWh · 76,245 miles · electricity £9,027 vs petrol equivalent £17,178 → **lifetime saving £8,152** · 11.84 p/mile electric vs 22.53 p/mile petrol.
>
> The open questions in §17 are annotated below with their outcomes.

---

## 1. Overview

ChargeWise is an open-source tool that tells a Tesla owner, accurately and at a glance, **what it actually costs to run their car on electricity** — and **how much they are saving versus petrol** — both right now and across the whole life of ownership (from February 2022 onwards for Alistair).

It does this by combining three data sources that already exist in Alistair's life:

- **TeslaFi** — charging sessions, energy added, location (home vs away), odometer/mileage.
- **Octopus Energy** (Intelligent Octopus Go) — the real half-hourly electricity rates and smart-charging dispatch slots that determine the true cost of each home charge.
- **GOV.UK weekly road fuel prices** — to value the equivalent petrol journey over the identical period.

There are two deliverables from one shared core:

1. **A Home Assistant dashboard** — for daily, in-home glanceability, reusing Alistair's existing Octopus and Tesla integrations.
2. **A standalone web dashboard** — independent of Home Assistant, with its own database, deployable to Azure cheaply, and shareable with the wider HA/Tesla community.

The product is built as an open-source project from day one: clean code, tests, documentation, and a permissive licence so other enthusiasts can run and contribute to it.

---

## 2. Problem statement

Tesla owners on smart EV tariffs genuinely cannot tell what their car costs to run. The information is scattered:

- TeslaFi knows *how much energy* went into the car and *where*, but it does not know the *price* Octopus actually charged at that moment.
- Octopus knows the *price* and the *smart-dispatch slots*, but does not attribute consumption specifically to the car.
- Neither side nets the result against *what petrol would have cost* over the same mileage and time.

The hard part is that **Intelligent Octopus Go pricing is dynamic**. The cheap off-peak rate applies to the whole home during the core window (23:30–05:30), but in *extra* daytime slots the cheap rate only applies *while the car is actively charging*. A naïve "kWh × headline rate" calculation is therefore wrong. Getting the cost right requires reconciling charge sessions against the actual rates and dispatch slots in force at the time.

The result today is that Alistair — and most owners — rely on guesswork or crude spreadsheets, and have no credible, ongoing answer to "what does the car cost me, and was going electric worth it?"

---

## 3. Goals and non-goals

### 3.1 Goals

- **G1.** Show the true cost of home charging, costed against actual Octopus rates and IOG dispatch slots at the time of each session.
- **G2.** Show the cost of away/public charging (Superchargers and third-party), from TeslaFi's data.
- **G3.** Show lifetime running cost since February 2022, combined across all Teslas owned in that period.
- **G4.** Show ongoing running cost (daily / weekly / monthly / per-charge) on a live basis.
- **G5.** Show savings versus an equivalent petrol car over the same mileage and period, using real historical fuel prices.
- **G6.** Present all of this in two clean, simple, trustworthy dashboards (HA + standalone) that do not look generic or "AI-generated".
- **G7.** Be a high-quality open-source product: tested, documented, MIT-licensed, easy for others to deploy.

### 3.2 Non-goals (v1)

- **NG1.** Not a full total-cost-of-ownership tool — insurance, servicing, tyres, depreciation, finance are explicitly **out of scope for v1** (energy only). *(Designed so cost categories can be added later.)*
- **NG2.** Not a charge-scheduling or vehicle-control tool — it reports cost, it does not command the car or change tariffs.
- **NG3.** Not a multi-energy-supplier product at launch — Octopus (and IOG specifically) is the first-class citizen; other tariffs are a future extension.
- **NG4.** Not tied to one specific car beyond Tesla/TeslaFi at launch.

---

## 4. Users and personas

**Primary — Alistair (owner-operator).** Technical, ADHD-aware, values clean visual dashboards over lists, wants minimal-friction "is it worth it?" reassurance and an accurate lifetime number. Runs Home Assistant on a Pi, comfortable with Docker and Azure.

**Secondary — the HA/Tesla enthusiast.** Runs Home Assistant, on an Octopus EV tariff, uses TeslaFi or similar, wants to deploy this themselves with their own credentials. Needs clear setup docs and sensible defaults.

**Tertiary — the contributor.** A developer who wants to extend the project (new tariffs, new data sources, new vehicles). Needs clean architecture, tests and contribution guidelines.

---

## 5. Scope (v1)

In scope: home charging cost (Octopus actual + dispatch-aware), away/public charging cost (TeslaFi), combined multi-vehicle lifetime totals from Feb 2022, live ongoing tracking, petrol-savings comparison (configurable, default ~30 mpg petrol), HA dashboard, standalone Azure-hosted dashboard with its own database, historical backfill via TeslaFi CSV import, MIT open-source release.

Out of scope (v1): the non-goals in §3.2, plus solar/battery integration, export/feed-in accounting, and non-UK fuel markets.

---

## 6. Functional requirements

Requirements are written as user stories with acceptance criteria. **MoSCoW** priority in brackets.

### 6.1 Data ingestion

- **FR-1 (Must).** *As the system, I back-fill historical charging (and drive) data from the TeslaFi history API (`command=charges`/`drives`, `dateFrom`/`dateTo`) so the back-history to Feb 2022 is captured.*
  AC: a documented backfill command pulls all charges across the date range; records are de-duplicated idempotently and attributed to a vehicle; re-running causes no duplication; **CSV import is available as a fallback** for periods/cars the API doesn't cover.

- **FR-2 (Must).** *As the system, I poll the TeslaFi history API on a schedule for new charge sessions so ongoing data appears without manual export.*
  AC: scheduled polling on a configurable interval (respecting TeslaFi rate limits); newly completed sessions are persisted; partial/in-progress sessions handled and finalised.

- **FR-3 (Must).** *As the system, I retrieve half-hourly electricity rates for the active Octopus tariff over any required date range so each charge can be priced accurately.*
  AC: rates fetched from the Octopus REST tariff endpoints for the account's historical tariff agreements; rates cached; VAT-inclusive pricing used for display, VAT-exclusive retained for reconciliation.

- **FR-4 (Must).** *As the system, I retrieve IOG smart-dispatch slots (completed dispatches) so daytime off-peak charging is costed correctly.*
  AC: dispatches retrieved via the Octopus GraphQL API (standalone path) or read from the HA Octopus integration (HA path); slots are matched to charge sessions by timestamp.

- **FR-5 (Should).** *As the system, I retrieve half-hourly home consumption for the meter so home charge energy can be cross-checked against TeslaFi's reported energy.*
  AC: consumption pulled from the Octopus REST consumption endpoint; used as a reconciliation/validation signal, flagged where it materially diverges from TeslaFi.

- **FR-6 (Must).** *As the system, I import GOV.UK weekly road fuel prices so petrol-equivalent costs can be computed for any week since Feb 2022.*
  AC: the published CSV(s) are ingested and refreshed weekly; petrol (ULSP) used by default; values stored in pence/litre by week.

### 6.2 Cost calculation (see §8 for methodology)

- **FR-7 (Must).** Each home charge session is costed against the actual rate(s) in force across its half-hour slots, with IOG dispatch awareness.
- **FR-8 (Must).** Each away/public charge session is costed from TeslaFi's recorded cost (or energy × a configurable away-rate where cost is absent).
- **FR-9 (Must).** Lifetime, monthly, weekly, daily and per-mile costs are derived and combined across all vehicles.
- **FR-10 (Must).** Petrol-equivalent cost and net saving are computed per period using miles driven × fuel price ÷ configurable mpg.
- **FR-11 (Should).** A "pence per mile" figure is produced for electric vs petrol, for the headline comparison.

### 6.3 Home Assistant dashboard

- **FR-12 (Must).** Headline cards: lifetime running cost, lifetime saving vs petrol, this-month cost, last-charge cost, p/mile electric vs petrol.
- **FR-13 (Should).** Trend cards: monthly cost over time; cumulative saving over time; home vs away split.
- **FR-14 (Must).** Reuses existing HA entities (Octopus rates/dispatch, Tesla) where possible; new derived values exposed via template sensors / helpers following HA best practice (no `.storage` edits, entity_id-based, correct helper choices).

### 6.4 Standalone dashboard

- **FR-15 (Must).** Works with no Home Assistant present, reading from its own database populated directly from the source APIs.
- **FR-16 (Must).** Views: Overview (headline numbers), History (filterable charge log), Savings (electric vs petrol over time), Settings (credentials, mpg, away-rate, tariff).
- **FR-17 (Should).** Date-range filtering and home/away filtering across all views.
- **FR-18 (Could).** CSV/PNG export of any chart or the charge log.

### 6.5 Configuration & onboarding

- **FR-19 (Must).** All secrets (TeslaFi token, Octopus API key, account number) are user-supplied via configuration, never committed.
- **FR-20 (Must).** First-run setup validates credentials and reports clearly what succeeded/failed.
- **FR-21 (Should).** Sensible defaults shipped (petrol ~30 mpg, away-rate fallback, IOG tariff) and all are user-overridable.

---

## 7. Data sources — integration design

| Source | Access | Used for | Key constraints |
|---|---|---|---|
| **TeslaFi** | **History API** `history.php?command=charges` & `command=drives` (Bearer token, `dateFrom`/`dateTo`); CSV export as fallback | Charge sessions, kWh added & used, home/travel/Supercharger location, charge cost, odometer, mileage, voltage/amps | History API is **beta** ("subject to change") — build defensively, keep CSV import as fallback. Exact JSON field names, multi-vehicle behaviour and rate limits to be confirmed with TeslaFi (see support email). |
| **Octopus REST** | `api.octopus.energy/v1`, API-key basic auth | Account/tariff agreements, half-hourly **unit rates**, half-hourly **consumption** | Rates/consumption are public-shaped but consumption needs auth; data can lag (SMETS collection delays). |
| **Octopus GraphQL** | `api.octopus.energy/v1/graphql`, token | **IOG completed/planned dispatches** | Required for accurate daytime off-peak costing; not in REST. |
| **Home Assistant (Alistair only)** | Existing BottlecapDave Octopus integration + Tesla Custom | Rates, dispatch slots, Tesla state — for the HA dashboard path | Already installed; avoids re-pulling APIs for the HA dashboard. |
| **GOV.UK weekly road fuel prices** | Published CSV (2003–2017, 2018–present), Open Government Licence | Petrol/diesel pence-per-litre by week | Weekly granularity; OGL attribution required. |

**Two ingestion paths, one model.** The HA dashboard can lean on entities Alistair already has; the standalone product pulls directly from Octopus REST + GraphQL and TeslaFi so it works for anyone without HA. Both write to / read from the same logical data model (§9).

---

## 8. Cost calculation methodology

This is the heart of the product and the main source of "right vs roughly right".

### 8.1 Home charging (Intelligent Octopus Go)

For each home charge session:

1. Split the session into half-hour slots aligned to Octopus settlement periods.
2. For each slot, determine the **applicable unit rate**:
   - If the slot is within the **core off-peak window** (23:30–05:30 local) → off-peak rate (whole-home cheap rate applies).
   - Else if the slot is covered by a **completed IOG smart-dispatch** and the car was charging → off-peak rate.
   - Else → the standard/peak rate in force for that slot from the tariff history.
3. Slot cost = energy in slot (kWh) × applicable rate. Energy per slot is taken from TeslaFi session data, apportioned across slots (with Octopus consumption used as a cross-check where available).
4. Session cost = Σ slot costs. Display VAT-inclusive; retain VAT-exclusive for reconciliation to Octopus bills.

This dispatch-aware approach is what makes the figure trustworthy rather than a headline-rate estimate.

### 8.2 Away / public charging

Use TeslaFi's recorded session cost where present. For Superchargers, prefer TeslaFi's **actual downloaded invoice cost** (TeslaFi retrieves Supercharger PDF invoices) over the estimated figure. Where TeslaFi has energy but no cost (common for some third-party chargers), apply a configurable away-rate (with a clearly-labelled "estimated" flag). Note TeslaFi's default cost for non-Supercharger charges is energy × a single per-kWh rate set in TeslaFi — which is why home charging is always re-costed against actual Octopus rates (§8.1).

### 8.3 Mileage and per-mile cost

Miles per period are derived from odometer deltas in TeslaFi drive/charge records. Electric p/mile = period energy cost ÷ period miles.

### 8.4 Petrol-equivalent and savings

For each period: petrol cost = (miles ÷ mpg) × litres-per-mile-conversion × GOV.UK petrol price for that week. Default **mpg = 30** (petrol), user-configurable. Saving = petrol cost − electric cost. Petrol p/mile = petrol price ÷ mpg (unit-converted). Headline saving = cumulative since Feb 2022.

### 8.5 Edge cases to handle

Tariff changes over time (use the agreement valid-from/to history); clock changes (BST/GMT — store UTC, present local); missing/late Octopus data; sessions spanning the off-peak boundary; multiple cars charging; vehicle changeover (one car sold, another bought).

---

## 9. Data model (logical)

- **vehicle** — id, name, VIN (optional), acquired_date, disposed_date.
- **charge_session** — id, vehicle_id, start/end (UTC), location_type (home/away), energy_kwh, odometer, source (teslafi_csv/teslafi_live), raw cost (if away).
- **charge_slot** — session_id, slot_start, energy_kwh, unit_rate, rate_source (core/dispatch/standard), cost.
- **tariff_agreement** — product_code, tariff_code, valid_from, valid_to.
- **unit_rate** — tariff_code, valid_from, valid_to, value_inc_vat, value_exc_vat.
- **dispatch_slot** — start, end, source (completed/planned).
- **consumption_halfhour** — meter, interval_start, consumption_kwh *(reconciliation)*.
- **fuel_price_week** — week_start, fuel_type, pence_per_litre.
- **drive / mileage** — vehicle_id, date, miles *(or derived from odometer deltas)*.
- **settings** — mpg, fuel_type, away_rate, tariff selection, credentials (stored securely, not in DB plaintext where avoidable).

Derived/aggregate views: cost_by_day / week / month, saving_by_period, lifetime_summary.

---

## 10. Architecture

**Shared core + two front-ends.**

- **Core service (standalone path):** a small containerised service that (a) ingests TeslaFi CSV + live, Octopus REST/GraphQL, and GOV.UK fuel CSV; (b) computes costs per §8; (c) stores to its own database; (d) serves an API + the standalone web dashboard.
- **Home Assistant path:** template sensors / helpers that surface the headline figures inside HA, computed from existing Octopus + Tesla entities (and/or by reading the core service's API). Built per HA best practices.

**Hosting (Azure, cheap & easy):**
- Compute: **Azure Container Apps (Consumption plan)** — scales to zero, generous free monthly grant, pay-per-use; ideal for a low-traffic personal app. *(Trade-off vs Container Instances/App Service noted in delivery plan.)*
- Database: **SQLite on a persisted volume** (Azure Files) for v1 — cheapest, fine for single-user. The data-access layer is abstracted (repository pattern) so a later move to **Azure Database for PostgreSQL Flexible Server (Burstable B1ms)** is a configuration change, not a rewrite.
- Scheduled ingestion: a timer-triggered job (Container Apps job or scheduled task) for live polling and weekly fuel-price refresh.

**Access model:** the standalone dashboard is **internet-reachable behind an authentication layer** (login required). This adds an auth requirement to the build (see §12) and means HTTPS, session/token handling and secret management are first-class.

**Tech stack (confirmed):** Python **FastAPI** for the core service/API and ingestion workers; **React / Next.js** for the standalone dashboard front-end; **SQLite** (abstracted data layer) for storage; **Docker** for portability (local Docker Compose and Azure use the same images); **Context7** used during build for up-to-date library documentation.

---

## 11. UX / UI principles

The explicit brief: **simple, clean, easy to use, and not looking like generic AI output.**

- Restrained, purposeful layout — a few confident headline numbers, not a wall of widgets.
- One clear story per screen: "what it cost" / "what I saved".
- Honest data: estimated figures are visibly labelled; no false precision.
- Calm, neutral visual language; consistent spacing and typography; charts that inform rather than decorate.
- Glanceable first, drill-down second. The lifetime saving and this-month cost should be readable in two seconds.
- Accessible: legible contrast, works on phone and desktop, no reliance on colour alone.
- HA dashboard should feel native to Home Assistant (Mushroom/standard cards), not bolted on.

A short visual style reference (colours, type scale, card patterns) will be produced in the design step before front-end build.

---

## 12. Non-functional requirements

- **Security & privacy.** All credentials user-supplied and kept out of source control; in production all secrets (Octopus API key, TeslaFi token, OAuth client secret, session secret) are stored in **Azure Key Vault** and injected into the container as environment variables via Key Vault references — never committed; local dev uses a gitignored `.env`. Data is personal and stays in the user's own deployment. No telemetry without explicit opt-in.
- **Authentication.** The standalone dashboard is internet-reachable, so it requires a login (authenticated sessions/tokens, HTTPS enforced, sensible password/session handling). Auth is pluggable so a self-hoster can use their own provider; a simple built-in login ships by default.
- **Reliability.** Ingestion is idempotent and resumable; API failures degrade gracefully; data gaps are surfaced, not hidden.
- **Performance.** Dashboards load quickly on a Pi-class device and a small Azure container; heavy historical recompute runs as a background job.
- **Maintainability.** Clear module boundaries (ingest / cost-engine / store / api / ui); the cost engine is pure and unit-testable in isolation.
- **Portability.** Runs locally via Docker Compose and on Azure with the same image.
- **Cost.** Target near-zero idle cost on Azure (scale-to-zero, SQLite default).
- **Licensing/compliance.** MIT for the project; GOV.UK data under OGL with attribution; respect TeslaFi/Octopus API terms and rate limits.

---

## 13. Open-source delivery

- Public GitHub repository, **MIT licence**.
- README with screenshots, feature list, and a clear "deploy your own" guide (local Docker + Azure).
- `CONTRIBUTING.md`, issue/PR templates, code of conduct.
- Configuration via `.env`/example files; **no secrets committed**; secret-scanning in CI.
- Versioned releases (semver) and a changelog.
- Documentation covering: setup, getting your TeslaFi/Octopus credentials, importing history, the cost methodology (so users trust the numbers), and troubleshooting.

---

## 14. Success metrics

- **Accuracy:** computed home-charging cost reconciles to within ~1–2% of Octopus bill figures over a test month.
- **Completeness:** lifetime total covers Feb 2022 → present with no unexplained gaps.
- **Trust:** estimated vs actual figures clearly distinguished; methodology documented.
- **Usability:** headline saving and monthly cost readable at a glance on both dashboards.
- **Adoptability:** a third party can deploy their own instance from the README without direct help.
- **Personal outcome:** Alistair has a single credible answer to "what has the Tesla cost, and what have I saved vs petrol?"

---

## 15. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| TeslaFi history API is beta ("subject to change") | Ingester could break on a field/format change | Build defensively against a pinned field set; keep CSV import as fallback; monitor for changes; confirm stability with TeslaFi support |
| IOG dispatch costing is complex | Wrong cost undermines trust | Dispatch-aware engine (§8); reconcile to bills; label estimates |
| Octopus data lag/gaps (SMETS) | Temporary missing figures | Treat consumption as cross-check not source of truth; surface gaps; rely on TeslaFi energy |
| Per-slot energy apportionment from TeslaFi | Slight cost inaccuracy at slot boundaries | Cross-check with Octopus consumption; document method; keep error small |
| Multiple cars / changeover | Double counting or gaps | Vehicle acquired/disposed dates; combined totals tested |
| Azure cost creep | Unexpected bills | Scale-to-zero, SQLite default, cost noted in docs |
| API terms / rate limits | Access risk | Respect limits, cache aggressively, back off on errors |
| Scope creep toward full TCO | Delay | TCO explicitly out of v1; extensible data model |

---

## 16. Assumptions and dependencies

- Alistair's Octopus account is on Intelligent Octopus Go with smart-dispatch data available (confirmed via HA integration).
- TeslaFi continues to permit token API access and monthly CSV export.
- The Tesla(s) charge predominantly at home via the IOG-controlled charger.
- GOV.UK continues to publish weekly road fuel prices under OGL.
- Petrol comparator default 30 mpg (Alistair's prior car ~29–32 mpg), user-configurable.
- Azure subscription available for hosting.

---

## 17. Decisions and remaining open questions

**Resolved:**
1. **Product name** — **ChargeWise**.
2. **Database default** — **SQLite** to start, data layer abstracted for a later Postgres move.
3. **Tech stack** — **FastAPI** core + **React/Next.js** front-end.
4. **Standalone access** — **internet-reachable with login** (auth layer required, §12).
5. **HA dashboard data** — HA **reads the ChargeWise API** (single source of truth). *Confirmed in the shipped v1: option (b) won — 16 REST sensors in `packages/chargewise.yaml` read the NAS-hosted API.*
6. **Auth provider** — **OAuth sign-in with Microsoft/Google** (no local passwords); pluggable for self-hosters.
7. **Standing charge** — **excluded** from charging cost (it is a fixed home cost, not a cost of charging).
8. **Vehicles** — Tesla #1 from **Feb 2022 to ~early August 2024**; Tesla #2 from **early August 2024 to present**. Combined lifetime totals across both.
9. **GitHub** — repo under account **AlistairWDoran**, public, MIT.
10. **Azure** — host on Azure Container Apps in subscription **Development-01** (`ca9e52f7-aedc-4f14-b84d-21a9e978cff7`); preferred region **UK South** (to confirm). Secrets in **Azure Key Vault**.
11. **Octopus** — account number **A-A8D3E32A**; v1 uses the **API-key** path (no OAuth registration). OAuth registration details captured in `OCTOPUS-OAUTH-SETUP.md` for a possible future "connect your account" flow.
12. **Auth (Entra ID)** — Microsoft sign-in via Entra tenant `alistairdoranoutlook.onmicrosoft.com` (admin `a-doran@alistairdoranoutlook.onmicrosoft.com`). App registration still to be created (yields client ID + secret); tenant ID (GUID) still needed.

**Still open (to resolve during build):**
- **TeslaFi history API specifics** — endpoint confirmed (beta). Awaiting TeslaFi support (see `TESLAFI-SUPPORT-EMAIL.md`): exact JSON field names, multi-vehicle behaviour, pagination, rate limits, whether Supercharger actual-invoice cost is included, and beta change-notification.
  > **ANSWERED (12 Jul 2026) — empirically; TeslaFi support never replied.** History is available via `history.php?command=charges` back to Feb 2022, so no support answer was ever needed. Key findings: `date` is UTC; `dateTo` is inclusive (capped at now); omitting dates returns full history; results arrive **non-chronologically** (sort client-side); responses can be **partial under throttling** (checksum `len(results)` vs `count`, retry); ~30 rapid calls then HTTP 429; `vin` field splits vehicles; `chargerKWH` = wall energy; `command=drives` returns 0 results; `command=chargeSummary` does not exist. Full detail in the resolution header of `TESLAFI-SUPPORT-EMAIL.md`. Note the Jan–mid-May 2026 data gap is a subscription lapse, not an API limitation.
- **Azure tenant ID (GUID)** and **Entra app registration** (client ID + secret) — needed to wire Microsoft OAuth login.
  > **DEFERRED (12 Jul 2026).** The auth provider decision is parked along with the internet-facing scope (standalone web app + Azure hosting). v1 shipped LAN-only with no login required.
- **Octopus API key** — to be placed in Key Vault / local `.env` (never in chat) to enable live ingestion and the golden reconciliation test.
  > **RESOLVED (12 Jul 2026) in part.** Key lives in the gitignored `backend/.env` and live ingestion runs daily on the NAS. The golden reconciliation test against a real Octopus bill remains the next accuracy milestone (see `PROJECT-STATUS.md`).

---

## 18. Future scope (post-v1)

Total cost of ownership (insurance, servicing, tyres, MOT, depreciation, finance); other Octopus tariffs and other suppliers (Agile, Go, generic time-of-use); other vehicles/loggers (Teslamate, native Tesla Fleet API); solar/battery and export accounting; carbon/CO₂ savings alongside cost; multi-currency/non-UK fuel markets; shareable public "savings" snapshot.

---

## 19. Glossary

**IOG** — Intelligent Octopus Go (smart EV tariff). **Dispatch** — an Octopus-scheduled smart-charging slot, often outside the core window, billed at off-peak while charging. **Half-hour / settlement slot** — the 30-minute period electricity is metered and priced in. **ULSP** — unleaded petrol (GOV.UK series). **p/mile** — pence per mile. **OGL** — Open Government Licence. **TCO** — total cost of ownership.

---

*End of PRD v0.1 — for review. On approval, this feeds the Delivery Plan, technical specifications and development harness (see `DELIVERY-PLAN.md`).*
