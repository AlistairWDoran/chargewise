"""scripts/deploy-nas.py reads its deploy target from configuration.

Host, port, user, key path and destination come from NAS_* environment
variables, with ``local/deploy.env`` (git-ignored) filling in any that are
unset. ``paramiko`` is stubbed, so the tests make no connection.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-nas.py"
NAMES = ("NAS_HOST", "NAS_SSH_PORT", "NAS_USER", "NAS_SSH_KEY", "NAS_DEST")

# Example values; the address is from the documentation range.
EXAMPLE = {
    "NAS_HOST": "192.0.2.10",
    "NAS_SSH_PORT": "2222",
    "NAS_USER": "deployer",
    "NAS_SSH_KEY": r"~\.ssh\example_key",
    "NAS_DEST": "/srv/chargewise",
}


@pytest.fixture
def deploy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "paramiko", types.ModuleType("paramiko"))
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("deploy_nas", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_env(path: Path, values: dict[str, str]) -> str:
    lines = ["# deploy target", ""] + [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_every_deploy_setting_comes_from_configuration(deploy) -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text) is None   # the host is configuration
    for constant in ("HOST", "PORT", "USER", "KEY", "DEST"):
        assert not hasattr(deploy, constant)
    assert deploy.REQUIRED == NAMES


def test_values_come_from_the_env_file(deploy, tmp_path) -> None:
    env_file = write_env(tmp_path / "deploy.env", EXAMPLE)
    assert deploy.load_config(env_file) == EXAMPLE   # backslashes in the key path survive


def test_environment_wins_over_the_env_file(deploy, tmp_path, monkeypatch) -> None:
    env_file = write_env(tmp_path / "deploy.env", EXAMPLE)
    monkeypatch.setenv("NAS_HOST", "nas.example")
    config = deploy.load_config(env_file)
    assert config["NAS_HOST"] == "nas.example"
    assert config["NAS_DEST"] == EXAMPLE["NAS_DEST"]


def test_environment_alone_is_enough(deploy, tmp_path, monkeypatch) -> None:
    for name, value in EXAMPLE.items():
        monkeypatch.setenv(name, value)
    assert deploy.load_config(str(tmp_path / "absent.env")) == EXAMPLE


def test_quotes_comments_and_a_bom_are_tolerated(deploy, tmp_path) -> None:
    path = tmp_path / "deploy.env"
    path.write_text(
        '﻿# comment\nNAS_HOST = "192.0.2.10"\nNAS_SSH_PORT=2222\n\n'
        "NAS_USER='deployer'\nNAS_SSH_KEY=~\\.ssh\\example_key\nNAS_DEST=/srv/chargewise\n",
        encoding="utf-8",
    )
    assert deploy.load_config(str(path)) == EXAMPLE


def test_missing_variables_are_named(deploy, tmp_path) -> None:
    partial = {k: v for k, v in EXAMPLE.items() if k not in ("NAS_SSH_PORT", "NAS_DEST")}
    env_file = write_env(tmp_path / "deploy.env", partial)
    with pytest.raises(SystemExit) as excinfo:
        deploy.load_config(env_file)
    message = str(excinfo.value.code)
    assert "missing NAS_SSH_PORT, NAS_DEST" in message
    assert "NAS_HOST" not in message
    assert env_file in message


def test_nothing_configured_names_all_five(deploy, tmp_path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        deploy.load_config(str(tmp_path / "absent.env"))
    assert all(name in str(excinfo.value.code) for name in NAMES)


def test_port_must_be_a_number(deploy, tmp_path) -> None:
    env_file = write_env(tmp_path / "deploy.env", {**EXAMPLE, "NAS_SSH_PORT": "ssh"})
    with pytest.raises(SystemExit, match="NAS_SSH_PORT must be a port number"):
        deploy.load_config(env_file)
