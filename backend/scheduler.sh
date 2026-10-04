#!/bin/sh
# ChargeWise daily ingestion loop — run by the chargewise-scheduler container.
#
# Lives in a real script (not a compose command string) because YAML folding
# mangled the inline version into invalid sh (the "sh: 6: || unexpected"
# crash-loop of 11 Jul 2026).
#
# Each cycle: fetch a rolling 35-day TeslaFi window (idempotent upsert
# re-costs existing sessions as new rates/dispatches arrive), refresh
# Octopus rates + dispatches and GOV.UK fuel prices, then sleep a day.
#
# Vehicle names come from the VEHICLE_MAP setting in the container's
# environment (.env): VEHICLE_MAP="VIN1=Name 1;VIN2=Name 2".
#
# A failed run must be impossible to miss: it is flagged here in the log
# (stderr, with the exit code) and the pipeline itself records which source
# failed and why, which /api/status reports as last_error / healthy: false.
# That includes a run the pipeline refuses to start because VEHICLE_MAP is
# malformed: it is recorded against the TeslaFi source before the pipeline
# exits. The loop carries on regardless, so one bad day does not stop the
# next run.

while true; do
  echo "[scheduler] pipeline run starting $(date -u +%FT%TZ)"
  if python -m chargewise.ingest.pipeline --teslafi \
    --from "$(date -d '35 days ago' +%F)"
  then
    echo "[scheduler] pipeline run OK $(date -u +%FT%TZ)"
  else
    status=$?
    {
      echo "[scheduler] ============================================================"
      echo "[scheduler] ERROR: pipeline run FAILED (exit $status) $(date -u +%FT%TZ)"
      echo "[scheduler] ERROR: steps after the failing one did not run. The cause is in"
      echo "[scheduler] ERROR: the output above and in /api/status (last_error)."
      echo "[scheduler] ERROR: retrying in 24h"
      echo "[scheduler] ============================================================"
    } >&2
  fi
  echo "[scheduler] sleeping 24h"
  sleep 86400
done
