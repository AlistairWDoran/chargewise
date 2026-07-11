"""Deploy ChargeWise to the Synology NAS over SSH (no SFTP required).

DSM's SFTP subsystem is off by default, so files are streamed through exec
channels instead (tar/cat over stdin). Unprivileged staging only — the final
`docker-compose up` needs sudo and is run by Alistair interactively.

Usage:  python scripts/deploy-nas.py            # stage files to the NAS
"""

from __future__ import annotations

import os
import sys

import paramiko

HOST, PORT, USER = "192.168.1.18", 49153, "admin"
KEY = os.path.expanduser(r"~\.ssh\id_ed25519")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = "/volume1/docker/chargewise"


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
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, key_filename=KEY,
                   timeout=10, look_for_keys=False, allow_agent=False)

    _, out, _ = client.exec_command(f"mkdir -p {DEST}/data && echo staged_dir_ok")
    print(out.read().decode().strip())

    bundle = os.path.join(os.environ["TEMP"], "chargewise-nas.tgz")
    stream(client, bundle, f"cd {DEST} && tar -xzf -")
    stream(client, os.path.join(REPO, "backend", ".env"),
           f"cat > {DEST}/.env && chmod 600 {DEST}/.env")
    stream(client, os.path.join(REPO, "backend", "data", "chargewise.sqlite"),
           f"cat > {DEST}/data/chargewise.sqlite")

    _, out, _ = client.exec_command(
        f"ls -la {DEST}; echo ---; ls {DEST}/backend | head -6; "
        f"echo ---; du -h {DEST}/data/chargewise.sqlite"
    )
    print(out.read().decode())
    client.close()


if __name__ == "__main__":
    main()
