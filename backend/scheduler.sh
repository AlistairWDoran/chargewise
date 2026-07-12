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

while true; do
  echo "[scheduler] pipeline run starting $(date -u +%FT%TZ)"
  python -m chargewise.ingest.pipeline --teslafi \
    --from "$(date -d '35 days ago' +%F)" \
    --vehicle-map "LRWYHCEK6NC223329=Tesla 1" \
    --vehicle-map "XP7YHCEK0RB479220=Friday" \
    || echo "[scheduler] pipeline run FAILED $(date -u +%FT%TZ) - retrying tomorrow"
  echo "[scheduler] sleeping 24h"
  sleep 86400
done
