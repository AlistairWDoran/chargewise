# ChargeWise — Project Status & Session Handoff

**Updated:** 12 July 2026 · **Profile:** Personal · **Repo:** https://github.com/AlistairWDoran/chargewise (Public, MIT, default branch `master`)
**Working folder:** `C:\Users\alist\OneDrive\Repos\Home-Automation\Home Automation\ev-charging-cost-tracker`

This is the single place a fresh session should read first.

## 1. What ChargeWise is

Open-source tool showing the true cost of running a Tesla on electricity and the savings vs petrol — ongoing and lifetime since Feb 2022. Combines TeslaFi (energy, location, mileage), Octopus Intelligent Octopus Go (real rates + smart-dispatch slots), and GOV.UK weekly fuel prices. See `PRD.md`, `DELIVERY-PLAN.md`, `METHODOLOGY.md`.

## 2. IT'S LIVE (11 Jul 2026)

**Production:** Docker on the Synology NAS (24/7) — `chargewise-api` (port 8000) + `chargewise-scheduler` (daily incremental pipeline, 35-day rolling re-cost). Deployed via `docker-compose.nas.yml` at `/volume1/docker/chargewise/`.
**Consumer:** Home Assistant dashboard "ChargeWise" (`/chargewise-ev/chargewise`) — 16 REST sensors via `packages/chargewise.yaml`, secrets point at `http://192.168.1.18:8000`.

**Lifetime figures (Feb 2022 → 12 Jul 2026, agent-verified 16/16 checks):** 4,403 sessions · 26,735 kWh · 76,245 miles · electricity £9,027 (home £7,869 / public £1,158) · petrol equivalent £17,178 · **saving £8,152** · 11.84 vs 22.53 p/mile.

**Scheduler:** WORKING since 12 Jul — daily loop in `backend/scheduler.sh` (an inline YAML-folded compose command arrived as invalid sh and crash-looped; never inline shell loops in compose). First run inserted the "missing" 5–11 July charges. `/api/status` exposes per-source last-sync + data freshness; HA "Status & data freshness" section shows TeslaFi/Octopus/fuel sync ages, latest charge and fuel week.

## 3. Environment & access

- **Windows-MCP** — PowerShell/files on Alistair's laptop (OpenSSH client + ssh-keygen do NOT work under it — use paramiko via `C:\Users\alist\.venvs\chargewise\Scripts\python.exe`).
- **Synology NAS** — 192.168.1.18, SSH port **49153**, user `admin`, key `C:\Users\alist\.ssh\id_ed25519`. **No passwordless sudo (Alistair's explicit choice)** — stage files as admin (see `scripts/deploy-nas.py`, streams over exec channels; SFTP disabled on DSM), then hand Alistair one `sudo env PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/docker-compose ...` command to run himself. Docker 20.10.3 + compose 1.28.5 live in `/usr/local/bin` (sudo strips it from PATH).
- **Home Assistant** — Pi 4 at 192.168.1.225:8123 (HA 2026.7.1). Config edits via the **File Editor add-on driven through Claude-in-Chrome** (ACE editor exposed at the iframe's `window.editor`; `loadfile`/`save_check`/`newfolder`/`newfile`). HA MCP in Cowork exposes only update/radio tools. Dashboards can be created over websocket (`lovelace/dashboards/create` — url_path needs a hyphen; modern sections format: `type: grid` + `heading` cards).
- **TeslaFi** — history API works: `history.php?token=…&command=charges&dateFrom&dateTo`. `date` is UTC; `chargerKWH` = wall energy; `vin` splits vehicles; rate-limits after ~30 rapid calls (adapter backs off). **Results arrive NON-chronologically and responses can be partial under throttling** — the adapter sorts client-side and checksums `len(results)` vs `count` with retry (a truncated probe caused a false "TeslaFi stopped recording" diagnosis on 11 Jul; always verify such claims with a fresh subagent probe). `dateTo` is inclusive; omitting dates returns full history. Token in `backend/.env` (`TESLAFI_TOKEN`).
- **Octopus** — key + account in `backend/.env` (`OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT_NUMBER`).

## 4. Hard-won lessons (do not relearn)

- **Rates arrive in PENCE** — `derive_iog_rate_periods` divides by 100; regression-tested. The first backfill priced lifetime charging at £784k because tests had encoded the pence assumption. Golden reconciliation against a real bill is still the missing trust anchor.
- Upsert **re-costs** existing sessions on re-run (idempotent by vehicle+start+energy, refreshes cost fields).
- Octopus GraphQL URL needs a trailing slash (301 on POST otherwise); zero-length tariff agreements must be skipped (400).
- Synology home-dir ACLs break SSH key auth ("bad ACL permission") — fix parent `homes` via File Station permission rewrite, then `synoacltool -del` + `chmod 755` on the home.
- Windows tar's pax headers break the NAS's GNU tar — build bundles with Python `tarfile` (GNU_FORMAT).

## 5. Accuracy caveats (honest state)

- **Dispatch-feed caveat (conservative):** Octopus dispatch feed is recent-window only (10 dispatches), so historical daytime smart-charges are billed at peak — overstates home costs. **Rate-era collapse (direction not guaranteed):** `derive_iog_rate_periods` reduces each tariff agreement to one min/max pair — peak slots overpriced, off-peak slots potentially underpriced (2022–24 VAR era approximate).
- **TeslaFi data gap Jan–mid-May 2026** — subscription lapse (confirmed by Alistair); unrecoverable from TeslaFi.
- Golden reconciliation test vs a real Octopus bill: **still to do** — next accuracy milestone.

## 6. Open items

1. **Golden reconciliation test** (accuracy gate from the delivery plan).
2. Multi-era rate refinement (per-era rates rather than min/max collapse).
3. mypy debt in older files (22 errors); new code is clean.
4. Next.js frontend + Azure deploy — **deliberately parked**; LAN v1 shipped. Revisit only if still wanted.
5. Vehicles named "Tesla 1" (LRW…3329) and "Friday" (XP7…9220) via `--vehicle-map`.

## 7. Daily operation

The NAS scheduler re-runs the pipeline daily (35-day window, fuel refresh included). Manual run on the NAS:
`ssh -t -p 49153 admin@192.168.1.18` then `cd /volume1/docker/chargewise && sudo env PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/docker-compose -f docker-compose.nas.yml run --rm scheduler python -m chargewise.ingest.pipeline --teslafi --from <date> --vehicle-map "LRWYHCEK6NC223329=Tesla 1" --vehicle-map "XP7YHCEK0RB479220=Friday"`.
Redeploy after code changes: rebuild bundle (`tarfile`, see `scripts/deploy-nas.py`), run the script, then Alistair runs the compose up command above with `up -d --build`.
