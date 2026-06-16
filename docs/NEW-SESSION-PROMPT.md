# New-session kickoff prompt

Paste the block below into a fresh Cowork session to continue ChargeWise exactly where we left off.

---

Switch to my **Personal** profile (load it from `C:\Users\alist\OneDrive\Repos\Profiles\profiles\personal\`).

We're continuing **ChargeWise**, my open-source EV charging cost & savings tracker. The project lives at:
`C:\Users\alist\OneDrive\Repos\Home-Automation\Home Automation\ev-charging-cost-tracker`
and on GitHub at `https://github.com/AlistairWDoran/chargewise`.

Before doing anything, read these for full context:
- `docs/PROJECT-STATUS.md` (start here — status, environment, decisions, open items)
- `docs/PRD.md`, `docs/DELIVERY-PLAN.md`, `docs/METHODOLOGY.md`
- `docs/TESLAFI-SUPPORT-EMAIL.md`, `docs/OCTOPUS-OAUTH-SETUP.md`

Tools available and how to use them:
- **Windows-MCP** — run `git`, `gh`, `az`, `azd`, `pytest` on my actual machine (gh is authenticated; repo remote is set). Use this for repo and deploy work.
- **Home Assistant MCP** — my HA at 192.168.1.225:8123 (Octopus + Tesla integrations live).
- **Context7** — current library docs. **Microsoft Learn MCP** — Azure guidance.
- Note: the OneDrive folder can mis-sync to the Linux sandbox; run tests on my machine via Windows-MCP or from a clean copy.

What's already built and passing (29 tests): the cost engine, Octopus REST/GraphQL + GOV.UK fuel ingest adapters, SQLite storage + repositories, and the FastAPI API. Scaffolding + CI are in the repo.

Pick up at the next step — my preference is the **Home Assistant dashboard** (reads the API) or the **Octopus + fuel ingestion pipeline**. Confirm the plan briefly, then proceed. Ask me for anything you need (e.g. Octopus API key goes into Key Vault / `.env`, never chat; Azure tenant ID; Entra app registration).

---
