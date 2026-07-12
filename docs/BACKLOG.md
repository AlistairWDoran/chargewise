# ChargeWise — Backlog

**Updated:** 12 July 2026 · Priorities set with the Mentor lens: trust first, then insight, then reach.

## P1 — Trust (do before anything else)

1. **Golden reconciliation test.** Price one full month of home charges with the engine and reconcile to the corresponding real Octopus bill (target ±1–2%). This is the project's accuracy gate (DELIVERY-PLAN §7) and the standing lesson from the pence bug: green unit tests prove consistency, not correctness. Needs: one Octopus bill (PDF or figures) from Alistair; the rest is a fixture + CI assertion.
2. **Multi-era rate refinement.** Replace `derive_iog_rate_periods`'s min/max collapse with one `RatePeriod` per Octopus pricing era, so the 2022–24 variable-tariff years are priced at the rate that actually applied. Corrects the £8,152 headline — direction not guaranteed: peak-billed slots are currently overpriced but off-peak slots may be underpriced where rates rose within an agreement.

## P2 — Insight

3. **Period summaries API** (`GET /api/summary/period?from&to&group=day|week|month`) — already sketched in DELIVERY-PLAN §4.4. Unlocks a real monthly savings trend to replace the flat lifetime-total history graph.
4. **HA monthly trend card** consuming (3) — apexcharts-card or a statistics graph fed by a proper monthly sensor.
5. **Data-gap surfacing.** Lifetime totals silently span the Jan–mid-May 2026 TeslaFi gap; expose known gaps in `/api/status` and label affected figures (PRD's completeness principle).

## P3 — Hygiene

6. **mypy cleanup** (22 errors in `api/app.py`, `store/repositories.py`, older Octopus client) so strict CI goes green.
7. **Scheduler timing** — fixed daily run time (e.g. 04:45 local, after most IOG overnight charging) instead of "24h since container start".
8. **NAS DHCP reservations** for the NAS (192.168.1.18) and Pi (192.168.1.225) in the router (one-time manual step for Alistair).

## Parked (revisit only if still wanted)

- Next.js standalone web app (Overview/History/Savings/Settings).
- OAuth login (Microsoft/Google) + internet exposure.
- Azure Container Apps deployment (`azd` + Bicep).
- Postgres swap.

## Recently completed (12 Jul 2026)

v1 LIVE: NAS containers (api + daily scheduler), HA dashboard with sync-status tiles, TeslaFi history adapter (backoff + partial-response checksum), pence→GBP fix with regression tests, re-costing upsert, `/api/status`, agent-verified figures (16/16 checks). See PROJECT-STATUS.md.
