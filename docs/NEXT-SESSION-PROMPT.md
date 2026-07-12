# Next-session kickoff prompt

Paste the block below into a fresh Cowork session to continue ChargeWise where we left off (12 Jul 2026).

---

Switch to my **Personal** profile (load it from `C:\Users\alist\OneDrive\Repos\Profiles\profiles\personal\`).

We're continuing **ChargeWise**, my open-source EV charging cost & savings tracker — **live since 11 Jul 2026** on my Synology NAS with a Home Assistant dashboard. Project folder:
`C:\Users\alist\OneDrive\Repos\Home-Automation\Home Automation\ev-charging-cost-tracker`
GitHub: `https://github.com/AlistairWDoran/chargewise`.

**Before doing anything, read these for full context:**
- `docs/PROJECT-STATUS.md` — start here: live architecture, environment/access details, hard-won gotchas (TeslaFi API quirks, Synology SSH/ACL/sudo specifics, Windows-MCP limitations)
- `docs/BACKLOG.md` — prioritised remaining work (P1 first: golden reconciliation test, then multi-era rates)
- `docs/METHODOLOGY.md` §7–8 — accuracy caveats the P1 work addresses

**Working method — non-negotiable:**
1. **Use multiple agents.** Plan the session's tasks, then delegate independent work items to parallel sub-agents (Agent tool), each with a self-contained brief: ground-truth facts, file paths, what to change, what NOT to touch, and "report files changed + summary".
2. **Verification is always done by a separate agent.** No task is complete until a fresh agent — not the one that did the work — has independently verified it with its own probes (run the tests, hit the API, check the data, falsify the claim). Present me the verification report, not just the doer's word. This rule exists because a confident wrong conclusion once survived until a verification agent killed it.
3. **Anything needing my hands** (sudo on the NAS, HA restarts I should know about, credentials): give me numbered steps, one copy-paste block per step, what it does, expected output, and exactly what to paste back. Remember: no passwordless sudo on the NAS — consolidate privileged steps into one command for me.
4. Track the session with the task list; update `docs/PROJECT-STATUS.md` and `docs/BACKLOG.md` before we finish, and refresh this prompt file for the following session.

**This session's goal (from BACKLOG P1):**
1. **Golden reconciliation test** — I'll provide one month's real Octopus bill figures when asked; build the fixture and CI assertion that the engine reconciles to ±1–2%.
2. If the golden test passes (or while awaiting my bill figures): **multi-era rate refinement** — one RatePeriod per Octopus pricing era instead of the min/max collapse, then re-run the pipeline on the NAS (staging is `scripts/deploy-nas.py`; the rebuild is my one sudo command) and re-verify the lifetime figures with a separate agent.

Confirm the plan briefly, then proceed. Key access facts (details in PROJECT-STATUS): NAS SSH port 49153 user admin via paramiko (OpenSSH client doesn't work under Windows-MCP), HA at 192.168.1.225:8123 edited via File Editor add-on through the Chrome extension, API at http://192.168.1.18:8000.

---
