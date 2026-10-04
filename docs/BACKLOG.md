# ChargeWise — Backlog

**Updated:** 4 October 2026 · Priorities: trust first, then insight and resilience, then hygiene. Background for every P1 item is in `PROJECT-STATUS.md` §2–3.

## P1 — Trust (do before anything else)

1. **Record the first CI run on `master`.** The fix is on `master`, including the change to `.github/workflows/ci.yml` (run on `master`, golden gate as its own job). What CI did has not been recorded here: expect the backend job to fail on the mypy debt (item 15) and the golden-reconciliation job to pass, and write down what actually happens.
2. **Live alert drill.** The Home Assistant alert is installed, enabled and healthy, and a manual trigger ran cleanly, but it has not been seen to fire on the live instance. Its logic was exercised only in a separate Home Assistant core under a simulated clock. The drill needs a real problem state held for more than 30 minutes (the API unreachable, or `healthy: false`), a notification seen, then recovery and the notification seen to clear. Choosing a safe way to cause the problem is part of the task.
3. **Dispatch capture timing.** Octopus serves smart-charge dispatches only for a short time, and the daily job runs once a day, at whatever time the containers were last started. A rehearsal run in the small hours saw the previous evening's dispatch; whether a run later in the morning still sees it is unconfirmed. The answer is the `dispatches:` count in that run's scheduler log, which needs root to read. If evening dispatches expire before the next run they are missed for good, and those are the ones that change the cost. Likely fix: fetch and store dispatches several times a day, independently of the daily ingest.
4. **Bill reconciliation with real figures.** The golden reconciliation test (`backend/tests/test_golden_reconciliation.py`) is built and skips until it has data. It needs two numbers from one Octopus bill: kWh, and energy cost excluding the standing charge. `scripts/export-golden-fixture.py --mode whole-house` builds the fixture; it takes dispatches from the current feed and does not yet read the stored ones. The fixture it writes holds half-hourly household consumption, so it is git-ignored and kept locally: the test picks it up on the machine that holds it, and CI goes on running the synthetic fixtures. This is the only check independent of both the code and Octopus's published rates. Expect the computed cost to exceed the bill for any month before dispatches were stored (October 2026). A bill for a period on the current day/night tariff also answers item 5.
5. **Decide the VAT question.** The day/night tariff now in force publishes identical inc-VAT and ex-VAT prices. If the bill adds 5% on top, costs since the move to that tariff are understated by 5% and the rate derivation needs a correction for such tariffs. Note that `engine/reconcile.py` recovers the ex-VAT rate as inc-VAT ÷ 1.05, which does not hold for this tariff's published figures.
6. **Decide how to treat the gap miles.** The miles driven during the gap in the charge history sit on the first session after it, with no charging cost behind them, which overstates the saving. Options: estimate the missing electricity and show it as an estimate, or exclude those miles from the saving. Whichever is chosen, label it on the dashboard and in `METHODOLOGY.md`.

## P2 — Insight and resilience

7. **Alert on data freshness as well as sync health.** `healthy` only says each source's last run succeeded within 36 hours. It ignores how old the latest charge is, so a feed that syncs cleanly but returns nothing new still looks healthy.
8. **Alert hardening.** A failure that flaps between states never reaches the 30 minutes the automation requires, so it never alerts. Swapping in a phone `notify.*` action would push every 5 minutes while the problem lasts, so it needs rate limiting first.
9. **A single shared place for secrets.** The Octopus key is copied by hand to every place that needs it; one regeneration broke ChargeWise.
10. **Period summaries and monthly trend.** `GET /api/summary/period?from&to&group=day|week|month` (sketched in DELIVERY-PLAN §4.4), then a Home Assistant monthly trend card to replace the flat lifetime-total graph.
11. **Scheduler timing.** Retry sooner after a failed run (today it waits the full 24 hours), and run at a fixed time of day. Today the run time is whenever the container was last started. Settle this together with item 3.
12. **Surface known data gaps.** Expose the gap in the charge history in `/api/status` and label the affected figures. Follows from item 6.
13. **Name the remaining raw errors.** Octopus GraphQL failures are now named; a malformed Octopus or TeslaFi response still surfaces in `last_error` as a bare `KeyError` or JSON decode error.
14. **Mask secrets in the container log.** Recorded errors are masked, but the traceback the scheduler prints is not, and a credential sent as a URL parameter appears in HTTP error messages.

## P3 — Hygiene

15. **mypy debt** — 18 errors in 4 files under `strict`; the CI type-check step fails until they are cleared.
16. **Keep installation-specific values in configuration.** They are read from `.env` and `local/` (both git-ignored); new tests, scripts and documents use the placeholders listed in `local.example/README.md`.
17. **Keep the dashboard file and the live dashboard in step.** The live dashboard is in storage mode and had drifted from `ha/dashboards/chargewise.yaml`. They now hold the same cards, but the live one has its three newest cards at the end of the "Status & data freshness" section, so the order differs from the file. Reorder one of them, and decide which is the source of truth.

## Parked (revisit only if still wanted)

- Next.js standalone web app (Overview/History/Savings/Settings).
- OAuth login (Microsoft/Google) + internet exposure.
- Azure Container Apps deployment (`azd` + Bicep).
- Postgres swap.

## Recently completed (4 Oct 2026)

The code and the live system were each verified by agents separate from the authors (`PROJECT-STATUS.md` §2–3).

- **Go-live:** containers rebuilt from the fixed code; first run clean; `/api/status` reports `healthy: true`.
- **Repair of the live data:** charges backfilled across the failure, mileage recomputed, every home session re-priced; the repaired database installed, with the pre-repair database kept as a backup.
- **Home Assistant alerting installed:** package (20 REST entities and the alert automation) and dashboard (sync-problem badge, "Latest charge recorded" and "Daily sync" tiles, "Last run, per feed" card). The live drill is still open (item 2).
- **On `master`** (item 1).
- **Rates:** day/night tariffs supported; rate periods clipped to their tariff agreement (an early short-lived tariff had been pricing four years of home charging at its flat rate); one rate period per pricing era instead of a min/max pair per agreement; overlap and missing-rate errors named; unit rates paginated. Checked against Octopus's published rates for every half-hour since February 2022 with 0 mismatches.
- **Data integrity:** dispatches stored permanently in a new `dispatch` table; mileage no longer blanked by the 35-day window; upsert matches on (vehicle, start).
- **Vehicle identity:** a vehicle is looked up by VIN before name, and keeps its stored name unless `VEHICLE_MAP` names its VIN. A run with the map missing, incomplete or typed in another case adds no vehicle and no session for a car stored with its VIN. A malformed map stops the run before it starts, and a refused TeslaFi run shows on `/api/status` as that source's `last_error`.
- **Repair commands:** `python -m chargewise.maintenance repair-miles|recost [--dry-run]`.
- **Visibility:** per-source last attempt and last error, and `healthy`, on `/api/status`; secrets masked in recorded errors; scheduler reports failures on stderr with the exit code.
- **Tooling:** `scripts/check-rates-against-octopus.py`; golden reconciliation test scaffold (sessions and whole-house modes) and its CI job; deploy script fixes; `.gitattributes` keeping `*.sh` LF, and the bundle builder writing shell, Docker and compose files with LF endings; ruff pinned to the 0.15 series.
- **Documentation:** root README headline figures corrected; stale comments in the pipeline, the Home Assistant package and the fixture export script corrected.
- **Earlier (12 Jul 2026):** v1 live — NAS containers, Home Assistant dashboard, TeslaFi history adapter with back-off and partial-response check, pence-to-pounds fix, `/api/status`.
