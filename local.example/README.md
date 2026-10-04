# `local/` — per-installation notes and settings

Each installation of ChargeWise has its own hosts, logins and identifiers. The code reads them from two git-ignored places, and the tracked files use placeholders in their examples.

| What | Where it lives | Tracked by git? |
|---|---|---|
| Settings and credentials — API keys, Octopus account number, vehicle names by VIN (`VEHICLE_MAP`) | `.env` (copy `.env.example`) | No — `*.env` and `.env.*` are ignored |
| Environment notes — hosts, ports, users, paths, the commands you run | `local/` at the repo root | No — `/local/` is ignored |
| The deploy target for `scripts/deploy-nas.py` | `local/deploy.env` | No |
| Real-bill fixtures written by `scripts/export-golden-fixture.py` | `backend/tests/fixtures/golden/*.real.json` | No — ignored; the golden test runs them where they are |
| Code, tests, docs, examples | the repository | Yes; examples use the placeholders below |

## Setting it up

```bash
mkdir local
cp local.example/deploy.env.example local/deploy.env   # then fill in your values
```

Suggested files (only `deploy.env` is read by a script):

- `local/ENVIRONMENT.md` — your hosts, ports, users and key paths, and your own form of the commands in `docs/PROJECT-STATUS.md` §5.
- `local/deploy.env` — `NAS_HOST`, `NAS_SSH_PORT`, `NAS_USER`, `NAS_SSH_KEY`, `NAS_DEST` for `scripts/deploy-nas.py`. Environment variables of the same names take precedence.

## Placeholders used in examples and tests

| Kind | Placeholder |
|---|---|
| Hosts in prose | `<nas-host>`, `<ha-host>`, `<chargewise-host>` |
| Addresses in examples that must look like one | the documentation range `192.0.2.x` (e.g. `192.0.2.10`) |
| SSH port, user, deploy directory | `<ssh-port>`, `<nas-user>`, `<nas-dest>` |
| VINs | `TESTVIN0000000001`, `TESTVIN0000000002` |
| Octopus account, MPAN, meter serial | `A-00000000`, `0000000000000`, `00X0000000` |
| Paths | relative to the repo (`<repo>`) |

## When you add something

- A test needs an identifier: use a placeholder from the table above.
- A script needs a host, a path or a credential: read it from the environment or from `local/`, and fail with a message naming what is missing.
- A document needs a command: write it with the placeholders above, and keep your own form of it in `local/ENVIRONMENT.md`.
- Before committing, check that `git status` lists nothing under `local/` and no `.env`.
