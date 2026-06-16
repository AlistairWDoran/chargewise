# ChargeWise — Project Status & Session Handoff

**Updated:** 16 June 2026 · **Profile:** Personal · **Repo:** https://github.com/AlistairWDoran/chargewise (Public, MIT, default branch `master`)
**Working folder:** `C:\Users\alist\OneDrive\Repos\Home-Automation\Home Automation\ev-charging-cost-tracker`

This is the single place a fresh session should read first. It summarises what ChargeWise is, what's built, the environment/tools, decisions, and the next steps.

## 1. What ChargeWise is

Open-source tool showing the true cost of running a Tesla on electricity and the savings vs petrol — ongoing and lifetime since Feb 2022. Combines TeslaFi (energy, location, mileage), Octopus Intelligent Octopus Go (real rates + smart-dispatch slots, for accurate home-charge costing), and GOV.UK weekly fuel prices (petrol comparison). Two front-ends from one core: a Home Assistant dashboard and a standalone web app. See `PRD.md`, `DELIVERY-PLAN.md`, `METHODOLOGY.md`.

## 2. Environment & tools (verified this session)

- **Windows-MCP** — runs PowerShell + file ops on Alistair's real Windows machine. Use it to run `gh`, `git`, `az`, `azd`, `pytest` against the actual repo/machine. This is the Desktop-Commander-equivalent in Cowork (Desktop Commander itself is not connected by name).
- **Home Assistant MCP** — HA at `http://192.168.1.225:8123`; Octopus (BottlecapDave) + Tesla Custom integrations live.
- **Context7** — up-to-date library docs (FastAPI, Next.js, SQLAlchemy, Auth.js).
- **Microsoft Learn MCP** — Azure Container Apps / Key Vault deployment guidance.
- **Mermaid + GitHub helper MCP** — can push files/PRs to an existing repo (needs a `Github-Token`); cannot create repos. Prefer Windows-MCP `gh`/`git` for repo work.
- Sandbox Linux shell (`bash`) is separate from the user's machine; the OneDrive mount can serve **stale/truncated file copies mid-sync** — verify tests on the real machine via Windows-MCP, or from a clean copy.

## 3. Confirmed decisions

Name **ChargeWise** · scope v1 = charging energy only (home + public), standing charge excluded · home cost via real Octopus IOG rates, dispatch-aware · combined totals across vehicles · stack **FastAPI + React/Next.js**, **SQLite** (abstracted for Postgres) · **internet-reachable with OAuth login (Microsoft/Google)** · HA reads the ChargeWise API · hosting **Azure Container Apps + Key Vault** · **MIT**, public GitHub.

## 4. Key identifiers

- Octopus account **A-A8D3E32A**; tariff `E-1R-INTELLI-VAR-24-10-29-H` (region H); off-peak £0.069, peak £0.303714 inc VAT (as of 15 Jun 2026). v1 uses the API-key path (not OAuth — see `OCTOPUS-OAUTH-SETUP.md`).
- Vehicles: Tesla #1 **Feb 2022 → early Aug 2024**; Tesla #2 **early Aug 2024 → present**. Combined totals.
- Azure subscription **Development-01** = `ca9e52f7-aedc-4f14-b84d-21a9e978cff7`; region UK South (tbc).
- Entra tenant `alistairdoranoutlook.onmicrosoft.com` (admin `a-doran@…`) for Microsoft sign-in.
- GitHub account **AlistairWDoran**; `gh` authenticated on the machine.

## 5. Built and verified (29 passing tests)

- **Cost engine** (`backend/chargewise/engine/`) — pure, dispatch-aware IOG costing, away costing, petrol savings, mileage. Grounded on real rates/dispatches.
- **Ingest adapters** (`backend/chargewise/ingest/`) — Octopus REST (tariff agreements, unit rates → RatePeriod) + GraphQL (dispatches); GOV.UK fuel-price CSV parser.
- **Storage** (`backend/chargewise/store/`) — SQLAlchemy models, idempotent repositories, lifetime summary with savings.
- **API** (`backend/chargewise/api/`) — FastAPI `/health`, `/api/summary/lifetime`, `/api/charges`, `/api/settings`; OAuth-ready auth gate; config reads secrets from env (Key Vault in Azure).
- Repo scaffolding: README, MIT LICENSE, `.gitignore`, `.env.example`, `Makefile`, `docker-compose.yml`, GitHub Actions CI (ruff, mypy, pytest+coverage, gitleaks).

Run tests: `cd backend && python -m pytest` (use a clean copy or Windows-MCP if the OneDrive mount mis-syncs).

## 6. Open items / not yet done

- **TeslaFi history API** — awaiting support reply (field names, multi-vehicle, pagination, rate limits, Supercharger invoice cost, beta stability). Email sent. See `TESLAFI-SUPPORT-EMAIL.md`.
- **Ingestion pipeline** — wire adapters → engine → store (Octopus + fuel can be built now; TeslaFi after reply or via CSV fallback).
- **Golden reconciliation test** — needs the Octopus API key (into Key Vault / local `.env`).
- **HA dashboard** — YAML package + Lovelace reading the API (use home-assistant-best-practices skill + HA MCP).
- **Next.js frontend** — Overview/History/Savings/Settings; warm, themeable, light/dark (theme-factory/canvas-design).
- **Auth** — create Entra app registration (client ID + secret); need Azure tenant ID (GUID).
- **Azure deploy** — `azd` + Bicep for Container Apps + Key Vault + Azure Files (SQLite volume).

## 7. Suggested next step

Build the **Home Assistant dashboard** (reads the API; quick visible win) or the **Octopus + fuel ingestion pipeline** (gets real data flowing). Frontend after.
