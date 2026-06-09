#!/usr/bin/env bash
# ApexCore portable smoke check (Linux / Astra).
#
# Collects a quick portability report from an arbitrary machine: OS, Python,
# `apexcore` version/info/doctor (sensor backends) and the live /api/hardware
# + /api/system endpoints. No root required, no browser opened.
#
# Usage:
#   bash smoke_check.sh
# Result:
#   ./apexcore_smoke_<host>_<timestamp>.txt   <- send this file back.
#
# Optional env:
#   APEXCORE_BIN=/path/to/apexcore   (default: apexcore from PATH)
#   APEXCORE_SMOKE_PORT=8799         (temp webui port for the API probe)
set -u

TS="$(date +%Y%m%d_%H%M%S)"
HOST="$(hostname 2>/dev/null || echo host)"
OUT="apexcore_smoke_${HOST}_${TS}.txt"
PORT="${APEXCORE_SMOKE_PORT:-8799}"
APEX="${APEXCORE_BIN:-apexcore}"

section() { printf '\n===== %s =====\n' "$1"; }

{
  echo "ApexCore smoke check"
  echo "timestamp: $TS"
  echo "host:      $HOST"

  section "OS / runtime"
  ( . /etc/os-release 2>/dev/null; echo "os:     ${PRETTY_NAME:-unknown}" )
  echo "kernel: $(uname -r 2>/dev/null || echo '?')"
  echo "arch:   $(uname -m 2>/dev/null || echo '?')"
  echo "python: $(python3 -V 2>&1 || echo 'no python3')"

  section "apexcore --version"
  if command -v "$APEX" >/dev/null 2>&1 || [ -x "$APEX" ]; then
    "$APEX" --version 2>&1
  else
    echo "[!] '$APEX' not found in PATH — set APEXCORE_BIN or install the package."
  fi

  section "apexcore info"
  "$APEX" info 2>&1 || echo "[info failed]"

  section "apexcore doctor (sensor backends)"
  "$APEX" doctor 2>&1 || echo "[doctor failed]"

  section "live API probe (temp webui on :$PORT)"
  "$APEX" webui --port "$PORT" >/tmp/apexcore_smoke_webui.log 2>&1 &
  WPID=$!
  sleep 8
  if command -v curl >/dev/null 2>&1; then
    echo "--- GET /api/hardware ---"
    curl -s --max-time 8 "http://127.0.0.1:${PORT}/api/hardware" 2>&1; echo
    echo "--- GET /api/system ---"
    curl -s --max-time 8 "http://127.0.0.1:${PORT}/api/system" 2>&1; echo
  else
    echo "[curl missing — skipped API probe; webui log below]"
    tail -n 10 /tmp/apexcore_smoke_webui.log 2>/dev/null
  fi
  kill "$WPID" 2>/dev/null
  wait "$WPID" 2>/dev/null
} > "$OUT" 2>&1

echo "Done. Please send back: $(pwd)/$OUT"
