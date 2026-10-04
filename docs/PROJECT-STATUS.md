# ChargeWise — Project Status

**Updated:** 4 October 2026 · **Repo:** https://github.com/AlistairWDoran/chargewise (Public, MIT, default branch `master`)

This is the public project status: what ChargeWise is, its current state and figures, the incident of summer 2026, how it is operated, the lessons learned and what is still open.

**Installation-specific settings are read from `.env` and the git-ignored `local/` folder.** The commands in this document use placeholders such as `<nas-host>` and `<chargewise-host>`; an installation keeps its own values and environment notes in `local/ENVIRONMENT.md`. [`local.example/README.md`](../local.example/README.md) describes the convention.

## 1. What ChargeWise is

Open-source tool showing the true cost of running a Tesla on electricity and the saving against petrol, since February 2022. It combines TeslaFi (charge sessions, energy, location, odometer), Octopus Energy (the published rates of the tariff in force, plus smart-charge dispatch slots) and GOV.UK weekly fuel prices. See `PRD.md`, `DELIVERY-PLAN.md`, `METHODOLOGY.md`.

**Production:** Docker on a NAS, deployed from `docker-compose.nas.yml` — `chargewise-api` (port 8000) and `chargewise-scheduler` (`backend/scheduler.sh`: one pipeline run when the container starts, then one every 24 hours, each fetching the last 35 days from TeslaFi). The database is a SQLite file in the deployment's `data/` directory.
**Consumer:** a Home Assistant dashboard, fed by REST sensors in `ha/packages/chargewise.yaml`; its secrets point at `http://<chargewise-host>:8000`.

## 2. Current state: live and verified (4 Oct 2026)

A silent failure that ran from late July and a pricing error present since go-live were diagnosed, fixed and deployed (§3).

- **Deployment.** On 4 October the old containers were stopped, the repaired database installed, and both containers rebuilt from the fixed code and started. The new scheduler's first run synced fuel prices, Octopus and TeslaFi in 21 seconds with no errors, and `/api/status` reports `healthy: true`.
- **Verified live** by an agent separate from the authors, working from a copy of the live database and the live API's answers:
  - The live database is the installed file plus exactly one daily run: one new session, three new dispatches, and no pre-existing row changed in any column. The first session of the 35-day window kept its mileage.
  - All 4,485 home sessions matched an independent re-price to within £0.0001.
  - Every field of `/api/summary/lifetime` and `/api/status` matched the database.
- **Home Assistant.** The package and dashboard are installed (4 Oct): 20 ChargeWise REST entities plus the alert automation, which is on. `binary_sensor.chargewise_sync_problem` is off and the three last-error sensors read `OK`. **The alert has not yet been seen to fire on the live instance** — see `ha/README.md` for what was and was not tested.
- **Git.** The fix is on `master`, including the CI change (run on `master`, golden reconciliation gate as its own job). What CI did on its first run is not yet recorded here: expect the backend job to fail on the known mypy debt (18 errors) and the golden-reconciliation job to pass.
- **Tests.** 319 passed, 2 skipped. The 2 skipped are the real-bill reconciliation placeholders, which still await bill figures. A real-bill fixture is git-ignored and kept locally, so that reconciliation runs on the machine that holds it.

### Lifetime figures

| | Before repair (live until 4 Oct 2026) | **Live, 4 Oct 2026** |
|---|---|---|
| Sessions | 4,417 | **4,609** |
| Energy | — | **28,615.6 kWh** |
| Miles | 75,502.8 | **80,949.5** |
| Electricity total | £9,040.91 | **£3,848.79** |
| Petrol equivalent | £17,007.92 | **£18,338.08** |
| **Saving** | £7,967.01 | **£14,489.29** |
| Pence per mile, electric vs petrol | — | **4.75 vs 22.65** |
| Period covered | to July 2026 | Feb 2022 – 4 Oct 2026 |

The live column is what the API returned after the first run on 4 Oct; it moves with every charge. The July headline (£8,152 saved, "agent-verified 16/16") was wrong: those checks tested that the arithmetic was self-consistent, not that the rates were right. Do not quote it.

### Limits of the live figures

The rates and the arithmetic are verified (§2, §3). These four things are not settled, and all four bear on the headline:

1. **Home cost is an upper bound.** Smart-charge dispatches were never stored before October 2026 and Octopus no longer serves the old ones, so daytime home charging before then is priced at the standard rate. If some of it was in fact smart-dispatched, the true home cost is lower. Energy inside the 23:30–05:30 off-peak window is unaffected. Every dispatch seen is now stored on each daily run; whether one run a day catches them all is an open question (`BACKLOG.md`, item 3).
2. **Gap miles.** There is a gap of several months in the charge history: TeslaFi recorded no charges, and they cannot be recovered. The first session after the gap carries all the miles driven during it, with no charging cost behind them. The saving is therefore overstated by the unrecorded electricity for those miles. **Unresolved:** estimate that electricity, or exclude those miles.
3. **VAT on the current tariff. Unresolved.** The day/night tariff now in force publishes identical inc-VAT and ex-VAT prices. If the bill adds 5% VAT on top, costs since the move to that tariff are understated by 5%. To be settled against a bill.
4. **Not reconciled against a real Octopus bill.** The golden reconciliation test is still the missing trust anchor. It needs two numbers from one bill: kWh, and energy cost excluding the standing charge.

## 3. The incident (late July – 4 October 2026)

**Timeline**

| When | What happened |
|---|---|
| 11–12 Jul | v1 went live (NAS containers + Home Assistant dashboard). |
| Mid-July | A change set (golden reconciliation scaffold, multi-era rate derivation, deploy script fixes) was built, verified and staged to the NAS — but the containers were never rebuilt and nothing was committed. |
| Late July | The account moved to a day/night tariff: a version of Intelligent Octopus Go whose prices are published only as separate day and night rates. The standard unit-rates endpoint has nothing for it. |
| From the next day | ChargeWise found no rate for any charge after the move, so costing raised "No rate period covers …" on every daily run. The scheduler's `\|\| echo FAILED` caught it; it was recorded nowhere else. TeslaFi was fine throughout and held every charge. |
| The following weeks | Each run still wrote part of its window before raising, and the sliding 35-day window blanked the stored mileage of one session per day (43 sessions in all). The dashboard's saving fell day by day. |
| September | The Octopus API key was regenerated. Octopus issues one key per account, so ChargeWise's copy stopped working (HTTP 401) and the run began failing earlier, at the Octopus stage. |
| Late Sep – 4 Oct | Diagnosed and fixed. The repair (charges backfilled across the failure, mileage recomputed, home sessions re-priced) was run on a copy of the database against live Octopus and TeslaFi. |
| 4 Oct | Go-live: repaired database installed, containers rebuilt, Home Assistant alerting installed (§2). |

**Root causes**

1. **Day/night tariffs were not supported** — the trigger for the failure.
2. **An early, short-lived tariff shadowed the correct one.** Rate periods were built from rate *records* and never clipped to the tariff *agreement's* dates, and the engine prices a slot with the first period that covers it. The account's first tariff lasted only a few days, but its single flat rate record is published as valid for years afterwards. So about four years of home charging was priced at that flat rate instead of the Intelligent Octopus Go rates the account was actually on. This was wrong from go-live. The bill reconciliation test that would have caught it never ran, because no bill figures were supplied.
3. **Data integrity and visibility.** The sliding window wiped mileage; smart-charge dispatches were never stored, so any re-pricing lost them; the upsert identity included energy, risking duplicates; and nothing recorded or alerted on a failed run.

**Fixes**

- **Rates** (`ingest/octopus_rest.py`, `ingest/pipeline.py`): rate periods are clipped to their agreement; periods across agreements are sorted and an overlap raises `OctopusRatesOverlap`; day/night tariffs are supported; unit rates are paginated; an agreement with no usable rates raises `OctopusRatesUnavailable` naming the tariff; off-peak or peak is decided by time of day (a record starting inside 23:30–05:30 local is off-peak), never by comparing values. `fetch_rate_periods(rest_client, agreements)` runs against Octopus's public endpoints with no key.
- **Data integrity** (`store/repositories.py`, `ingest/pipeline.py`): a new `dispatch` table keeps every dispatch seen, and pricing uses all stored dispatches. A window's first session takes its mileage from the vehicle's last stored odometer, and an ingest never replaces a stored odometer or miles value with nothing. The upsert matches on (vehicle, start). A vehicle is looked up by VIN before name (added after go-live), so a vehicle stored with its VIN is found whatever display name comes with it.
- **Repair commands** (`chargewise/maintenance.py`): `repair-miles` and `recost`, each with `--dry-run`, both idempotent (§5).
- **Visibility:** each pipeline stage (fuel, Octopus, TeslaFi) records its last attempt and last error. `/api/status` gains per-source `last_attempt_utc` and `last_error`, and a top-level `healthy` — false if any source has an error or has not succeeded within 36 hours. Secrets are masked in recorded errors. Octopus GraphQL failures are named (`OctopusGraphQLError`). `scheduler.sh` reports a failed run on stderr with its exit code and keeps looping.
- **Alerting** (`ha/packages/chargewise.yaml`): `binary_sensor.chargewise_sync_problem` (fail-closed), three last-error sensors, and an automation that raises a persistent notification after 30 minutes of a sync problem or an unreachable API, and dismisses it on recovery.

**Verification of the fixes, before go-live** — by agents separate from the authors:

- The code's rate for every half-hour from the first recorded charge (February 2022) to 4 Oct 2026 — over 81,000 instants — matched Octopus's published records: 0 mismatches. Boundary, DST and 300,000 random-instant probes: 0 mismatches.
- All 4,294 home sessions then stored, re-priced by the code, matched an independent slot-by-slot re-price to within £0.0001. (The same check on the live database after go-live covered 4,485 — §2.)
- A 60-day replay of the daily job caused no drift in miles, sessions or saving. The same replay on the old code lost mileage.

## 4. Environment & access

Hosts and addresses, the SSH port and user, key and folder paths, the NAS layout, how Home Assistant is reached and where each secret lives differ from one installation to the next. The code reads them from `.env` and from the git-ignored `local/` folder, and an installation's own notes on them go in **`local/ENVIRONMENT.md`**; `local.example/README.md` describes the convention.

What applies to any installation:

- **Configuration** comes from the environment or a git-ignored `.env` (`.env.example` lists every setting). Containers read `.env` only when they are created, so a changed key or setting needs the containers recreated.
- **Vehicles** are identified by VIN; the name is a label. Names come from the `VEHICLE_MAP` setting in `.env` (`"VIN1=Name 1;VIN2=Name 2"`), or from `--vehicle-map` flags, which replace it. A vehicle already stored is found by its VIN and keeps its stored name unless the map names that VIN, so a run with the map missing or incomplete adds no vehicle and stores no charge twice for a car stored with its VIN. A malformed map stops every mode of the pipeline before anything is fetched (exit status 1), with the number of the entry at fault and the reason; a refused TeslaFi run — the daily job's — is recorded as that source's `last_error`, so `/api/status` turns unhealthy at once. A name may contain spaces, commas and apostrophes, but not `;`, `=` or a double quote, and may not start or end with an apostrophe.
- **TeslaFi** — history API: `history.php?token=…&command=charges&dateFrom&dateTo`. `date` is UTC; `chargerKWH` is wall energy; `vin` splits vehicles. It rate-limits after about 30 rapid calls (the adapter backs off). **Results arrive non-chronologically and can be partial under throttling** — the adapter sorts client-side and checks `len(results)` against `count`, retrying on a mismatch. `dateTo` is inclusive; omitting dates returns the full history. The token is the `TESLAFI_TOKEN` setting.
- **Octopus** — `OCTOPUS_API_KEY` and `OCTOPUS_ACCOUNT_NUMBER`. **Octopus issues one key per account: regenerating it on their site invalidates every copy.** When another tool needs the key, copy the existing one. The product and rate endpoints are public and need no key; the account's agreement list and the dispatch feed do.
- **Deploying to the NAS** — `scripts/deploy-nas.py` reads its target from `NAS_HOST`, `NAS_SSH_PORT`, `NAS_USER`, `NAS_SSH_KEY` and `NAS_DEST` (environment variables, or `local/deploy.env`). Only Docker on the NAS needs root; staging files does not.

## 5. Operations

Placeholders: `<chargewise-host>` is the machine serving the API; `<nas-host>`, `<ssh-port>` and `<nas-user>` are the NAS and its SSH login; `<nas-dest>` is the deployment directory there. An installation's own values are in its `local/ENVIRONMENT.md`.

**Daily run.** The scheduler runs the pipeline when its container starts and every 24 hours after that, so the time of day of the daily run is whenever the containers were last recreated. Order: fuel prices → Octopus (rates, dispatches) → TeslaFi (fetch the last 35 days, price, store). A failing stage stops the run, and the stages after it do not run. Costing happens inside the TeslaFi stage, so a pricing failure is reported against TeslaFi.

**Health check.** `GET http://<chargewise-host>:8000/api/status`: `healthy` should be `true`, each source's `last_error` `null`, and `teslafi.latest_charge_utc` recent. In Home Assistant, `binary_sensor.chargewise_sync_problem` should be off. `healthy` does not look at the age of the latest charge, so check that separately.

**Redeploy after code changes:** `python scripts/build-nas-bundle.py` (writes shell, Docker and compose files into the bundle with LF endings), then `python scripts/deploy-nas.py` (stages as `<nas-user>`; never touches `data/` or `.env` unless `--with-db` is given), then the one privileged step — rebuild the containers on the NAS:
`ssh -t -p <ssh-port> <nas-user>@<nas-host> "cd <nas-dest> && sudo docker-compose -f docker-compose.nas.yml up -d --build"`
(under `sudo` some systems need the full path to `docker-compose`). Recreating the scheduler container runs the pipeline straight away and resets the time of the daily run. Confirm with the health check.

**Manual run on the NAS** (root): from `<nas-dest>`,
`sudo docker-compose -f docker-compose.nas.yml run --rm scheduler python -m chargewise.ingest.pipeline --teslafi --from <date>`
Vehicle names come from `VEHICLE_MAP` in the deployment's `.env`.

**Repair commands.** The repair has been done; these remain for any future need. Both are idempotent, and `--dry-run` prints the before/after summary and writes nothing. On the NAS they run in the scheduler image, in the same form as the manual run above.

- `python -m chargewise.maintenance repair-miles` — recomputes every session's miles from the stored odometer readings.
- `python -m chargewise.maintenance recost` — re-prices every stored home session against current rates and all stored dispatches. It exits non-zero if any home session cannot be priced.
- A backfill beyond the daily 35-day window is the manual run above with an earlier `--from` date.

**Re-check the rates against Octopus:** `python scripts/check-rates-against-octopus.py <agreements.json>` compares the code's rate for every half-hour with Octopus's published records, using the public API and no key. The agreements file is a JSON list of `{tariff_code, valid_from, valid_to}`. It is account history — keep it outside the repo.

## 6. Hard-won lessons (do not relearn)

**Trust**

- **Verify rates against the supplier's published data, not against internal consistency.** "Verified 16/16" in July checked that the arithmetic agreed with itself, while every home charge was priced at the wrong rate. Say what a check compared before calling anything verified.
- **Never price from rate records without clipping them to the tariff agreement.** A published record can outlive the agreement it belongs to by years.
- **Rates arrive in pence.** The conversion to pounds happens once, in `octopus_rest.py`, and is regression-tested. The first backfill priced lifetime charging at £784k because the tests had encoded the same wrong assumption as the code.
- **A real bill is still the only independent anchor.** Green unit tests prove consistency, not correctness.
- **Installed is not the same as seen to work.** The alert automation is installed and was exercised under a simulated clock, but it has not fired on the live instance. Say which of the two has been shown.
- A confident diagnosis can be wrong: a truncated TeslaFi probe produced a false "TeslaFi stopped recording" conclusion in July. Have a separate agent verify such claims with its own probe.

**Operations**

- **A failing daily job must be visible.** `|| echo FAILED` in a container log is not visibility; the job failed for two and a half months unseen. Record the failure where a dashboard reads it, and alert on it.
- **One key per Octopus account.** Regenerating it for one tool breaks every other tool. Copy it.
- A staged change is not a deployed change: the July change set sat staged, unbuilt and uncommitted for eleven weeks. Finish with the rebuild and confirm it from `/api/status`.
- Containers read `.env` only when created.
- `scheduler.sh` must have LF line endings; with CRLF it breaks in the container, and on 4 Oct the copy in the working folder had CRLF. `.gitattributes` keeps `*.sh` LF in git, and the bundle builder now writes LF whatever the working folder holds.
- Never inline a shell loop in a compose `command:`. The YAML-folded version arrived as invalid `sh` and crash-looped; the loop lives in `backend/scheduler.sh`.
- A re-ingest of part of the history must never overwrite stored values it cannot derive. The 35-day window did exactly that to mileage.
- Identify a record by its stable key, not by a label someone can change. Vehicles are looked up by VIN; the display name is free to change.
- **Read an installation's settings from configuration.** API keys, the Octopus account number and vehicle names come from `.env`, and the deploy target from `local/deploy.env` (both git-ignored); tests and examples use placeholders.

**Platform quirks**

- The Octopus GraphQL URL needs a trailing slash (301 on POST otherwise). GraphQL reports failures, including a refused key, as HTTP 200 with an `errors` array. Zero-length tariff agreements must be skipped (the rates endpoint answers 400).
- On some NAS systems, home-directory ACLs break SSH key authentication ("bad ACL permission"); the fix is specific to the NAS's operating system.
- Windows tar's pax headers break the NAS's GNU tar: build bundles with Python `tarfile` (GNU_FORMAT), as `scripts/build-nas-bundle.py` does.
- A NAS may have SFTP switched off; `scripts/deploy-nas.py` streams files over SSH exec channels instead.
- Home Assistant: after a change to the package with no new secrets, `rest.reload` and `automation.reload` are enough. A reload does not re-resolve `!secret` values; adding or changing a secret needs a restart.
- Home Assistant: a dashboard edited in the UI (storage mode) and `ha/dashboards/chargewise.yaml` are separate copies and drift apart. On 4 Oct the live one lacked a tile the repo file had.

## 7. Open items

Priorities and detail are in `BACKLOG.md`. In short:

1. Record what CI did on its first run on `master`.
2. Live alert drill: see the Home Assistant notification fire and clear on the live instance.
3. Dispatch capture timing: does one run a day still see the previous evening's dispatches?
4. Reconcile against a real Octopus bill (two numbers needed from one bill).
5. Decide the VAT question (§2, limit 3).
6. Decide how to treat the gap miles (§2, limit 2).

The Next.js frontend, OAuth and Azure deployment remain deliberately parked.
