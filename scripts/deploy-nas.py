"""Deploy ChargeWise to a NAS over SSH (no SFTP required).

A NAS can ship with its SFTP subsystem switched off, so files are streamed
through exec channels instead (tar/cat over stdin). Unprivileged staging only —
the final `docker-compose up` needs root and is run by hand on the NAS.

The deploy target comes from five environment variables; any that are not set
are read from `local/deploy.env` (git-ignored, plain KEY=VALUE lines — see
local.example/deploy.env.example):

    NAS_HOST      host name or address of the NAS
    NAS_SSH_PORT  SSH port
    NAS_USER      SSH user that owns the destination directory
    NAS_SSH_KEY   path to the private key (a leading ~ is expanded)
    NAS_DEST      destination directory on the NAS

Usage:  python scripts/deploy-nas.py            # stage code only (default)
        python scripts/deploy-nas.py --with-db  # ALSO overwrite NAS DB + .env
                                                # (first deploy / deliberate reset
                                                # only — the NAS DB is the live
                                                # one once the scheduler runs)
"""

from __future__ import annotations

import os
import sys

import paramiko

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_ENV = os.path.join(REPO, "local", "deploy.env")
REQUIRED = ("NAS_HOST", "NAS_SSH_PORT", "NAS_USER", "NAS_SSH_KEY", "NAS_DEST")


def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines from ``path`` (an absent file gives nothing).

    Blank lines and ``#`` comment lines are skipped, and one pair of quotes
    around a value is dropped. Nothing else is interpreted: backslashes in a
    Windows path stay as they are.
    """
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def load_config(env_file: str = DEPLOY_ENV) -> dict[str, str]:
    """The NAS_* settings: the environment wins, ``local/deploy.env`` fills gaps.

    Exits with a message naming every variable that is missing.
    """
    from_file = read_env_file(env_file)
    config = {name: os.environ.get(name) or from_file.get(name, "") for name in REQUIRED}
    missing = [name for name in REQUIRED if not config[name]]
    if missing:
        sys.exit(
            f"deploy-nas: missing {', '.join(missing)}. Set the variable(s) in the "
            f"environment or in {env_file} (KEY=VALUE lines; see "
            "local.example/deploy.env.example)."
        )
    if not config["NAS_SSH_PORT"].isdigit():
        sys.exit(
            f"deploy-nas: NAS_SSH_PORT must be a port number, got {config['NAS_SSH_PORT']!r}."
        )
    return config


def stream(client: paramiko.SSHClient, local_path: str, remote_cmd: str) -> None:
    """Pipe a local file into a remote command's stdin."""
    stdin, stdout, stderr = client.exec_command(remote_cmd)
    with open(local_path, "rb") as fh:
        while chunk := fh.read(65536):
            stdin.write(chunk)
    stdin.channel.shutdown_write()
    rc = stdout.channel.recv_exit_status()
    err = stderr.read().decode()[:300]
    print(f"  {os.path.basename(local_path)} -> rc={rc}" + (f" err={err}" if err else ""))
    if rc != 0:
        sys.exit(1)


def main() -> None:
    config = load_config()
    dest = config["NAS_DEST"].rstrip("/")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(config["NAS_HOST"], port=int(config["NAS_SSH_PORT"]),
                   username=config["NAS_USER"],
                   key_filename=os.path.expanduser(config["NAS_SSH_KEY"]),
                   timeout=10, look_for_keys=False, allow_agent=False)

    _, out, _ = client.exec_command(f"mkdir -p {dest}/data && echo staged_dir_ok")
    print(out.read().decode().strip())

    bundle = os.path.join(os.environ["TEMP"], "chargewise-nas.tgz")
    # Clear the old code tree first: a previous bundle carried read-only
    # modes (r-xr-xr-x), and GNU tar cannot replace files inside a
    # non-writable directory ("Cannot open: File exists"). chmod as owner
    # always works; data/ and .env are never touched.
    stream(
        client,
        bundle,
        f"cd {dest} && chmod -R u+w backend 2>/dev/null; "
        f"rm -rf backend docker-compose.nas.yml && tar -xzf -",
    )
    if "--with-db" in sys.argv:
        print("  (--with-db: overwriting NAS database and .env)")
        stream(client, os.path.join(REPO, "backend", ".env"),
               f"cat > {dest}/.env && chmod 600 {dest}/.env")
        stream(client, os.path.join(REPO, "backend", "data", "chargewise.sqlite"),
               f"cat > {dest}/data/chargewise.sqlite")

    _, out, _ = client.exec_command(
        f"ls -la {dest}; echo ---; ls {dest}/backend | head -6; "
        f"echo ---; du -h {dest}/data/chargewise.sqlite"
    )
    print(out.read().decode())
    client.close()


if __name__ == "__main__":
    main()
