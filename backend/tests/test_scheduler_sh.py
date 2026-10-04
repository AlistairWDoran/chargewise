"""scheduler.sh: a failed pipeline run is flagged loudly and the loop carries on.

Runs the real script under ``sh`` with stub ``python``, ``sleep`` and ``date``
on PATH. The first pipeline run fails (exit 3), the second succeeds; the
``sleep`` stub ends the script after the second cycle.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scheduler.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="needs a POSIX sh to run scheduler.sh (not available on this platform)",
)

STUBS = {
    # Fails on its first call, succeeds afterwards; logs the arguments it was given.
    "python": """#!/bin/sh
n=$(($(cat "$STUB_DIR/python.calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$STUB_DIR/python.calls"
echo "$*" >> "$STUB_DIR/python.args"
if [ "$n" -eq 1 ]; then
  echo "Traceback (most recent call last): boom" >&2
  exit 3
fi
echo "ChargeWise ingestion complete:"
""",
    # Never sleeps; after the second cycle it stops the scheduler (its parent).
    "sleep": """#!/bin/sh
n=$(($(cat "$STUB_DIR/sleep.calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$STUB_DIR/sleep.calls"
echo "$*" >> "$STUB_DIR/sleep.args"
if [ "$n" -ge 2 ]; then
  kill -TERM "$PPID"
fi
""",
    # Fixed clock, so the output and the --from date are deterministic everywhere.
    "date": """#!/bin/sh
case "$*" in
  *"35 days ago"*) echo 2026-05-01 ;;
  *) echo 2026-06-05T04:00:00Z ;;
esac
""",
}


@pytest.fixture
def scheduler_run(tmp_path):
    for name, body in STUBS.items():
        stub = tmp_path / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    env = {**os.environ, "STUB_DIR": str(tmp_path),
           "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
    )
    return done, tmp_path


def test_loop_continues_after_a_failed_run_and_reports_it(scheduler_run):
    done, stub_dir = scheduler_run

    # Two full cycles ran: the failure did not end the loop.
    assert (stub_dir / "python.calls").read_text().strip() == "2"
    assert (stub_dir / "sleep.args").read_text().split() == ["86400", "86400"]
    assert done.stdout.splitlines() == [
        "[scheduler] pipeline run starting 2026-06-05T04:00:00Z",
        "[scheduler] sleeping 24h",
        "[scheduler] pipeline run starting 2026-06-05T04:00:00Z",
        "ChargeWise ingestion complete:",
        "[scheduler] pipeline run OK 2026-06-05T04:00:00Z",
        "[scheduler] sleeping 24h",
    ]

    # The failed run is unmistakable on stderr, after the pipeline's own output,
    # and carries the pipeline's exit code. The successful run adds nothing there.
    errors = done.stderr.splitlines()
    assert errors[0] == "Traceback (most recent call last): boom"
    assert "[scheduler] ERROR: pipeline run FAILED (exit 3) 2026-06-05T04:00:00Z" in errors
    assert sum("FAILED" in line for line in errors) == 1
    assert sum(line.startswith("[scheduler] ====") for line in errors) == 2
    assert any("/api/status" in line for line in errors)
    assert "run OK" not in done.stderr and "FAILED" not in done.stdout


def test_each_run_asks_for_the_rolling_window(scheduler_run):
    _, stub_dir = scheduler_run
    calls = (stub_dir / "python.args").read_text().splitlines()
    assert len(calls) == 2 and calls[0] == calls[1]
    # Vehicle names come from the VEHICLE_MAP setting, so these are all the arguments.
    assert calls[0] == "-m chargewise.ingest.pipeline --teslafi --from 2026-05-01"


def test_vehicle_names_are_left_to_the_setting():
    """The script leaves vehicle names to the VEHICLE_MAP setting."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--vehicle-map" not in text
    assert re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text) is None
    assert "VEHICLE_MAP" in text  # the comment says where the names come from


def test_failure_text_points_at_the_output_and_the_status_api(scheduler_run):
    done, _ = scheduler_run
    assert "[scheduler] ERROR: the output above and in /api/status (last_error)." in done.stderr


def test_script_is_posix_sh_with_lf_line_endings():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n")
    assert b"\r" not in raw
    assert subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True).returncode == 0
