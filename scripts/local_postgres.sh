#!/usr/bin/env bash
# Spin up a disposable local Postgres for the Postgres-gated tests (E27-T6 / DEBT-31).
#
# The backend/RLS/retention suites (adapters/backend-supabase, the parity + metrics
# tests) only run when KANTAQ_TEST_POSTGRES_URL points at a real server — otherwise
# they skip. In CI that server is a service container; locally this script gives you
# the same thing in one command, so backend epics are verifiable on your machine
# instead of via a 10-15 min CI round-trip.
#
# This is a *convenience*, not a gate: the real contract is the env var. If this
# script can't find Postgres on your box, set KANTAQ_TEST_POSTGRES_URL by hand at
# any reachable server and the tests run all the same.
#
# Usage:
#   eval "$(scripts/local_postgres.sh start)"   # start + export KANTAQ_TEST_POSTGRES_URL
#   make test-pg                                 # or: uv run pytest
#   scripts/local_postgres.sh stop               # tear the cluster down
#
# `start` prints a `export KANTAQ_TEST_POSTGRES_URL=...` line on stdout (so `eval`
# captures it); all human-facing logging goes to stderr.
set -euo pipefail

DATADIR="${KANTAQ_PG_DATADIR:-/tmp/kantaq-local-pg}"
PORT="${KANTAQ_PG_PORT:-54329}"
# Locale C: the suite asserts byte-ordering parity with the SQLite path (my notes /
# MOD-30 parity test); a non-C collation would skew text ordering. initdb bakes it in.
LOCALE="C"

log() { echo "[local_postgres] $*" >&2; }

find_pg_bin() {
  if [ -n "${KANTAQ_PG_BIN:-}" ]; then echo "$KANTAQ_PG_BIN"; return; fi
  local p
  for f in postgresql@15 postgresql@16 postgresql; do
    if command -v brew >/dev/null 2>&1; then
      p="$(brew --prefix "$f" 2>/dev/null || true)/bin"
      [ -x "$p/pg_ctl" ] && { echo "$p"; return; }
    fi
  done
  # Fall back to PATH.
  if command -v pg_ctl >/dev/null 2>&1; then dirname "$(command -v pg_ctl)"; return; fi
  log "ERROR: no Postgres found. Install one (brew install postgresql@15) or set KANTAQ_PG_BIN."
  exit 1
}

URL="postgresql+psycopg://postgres@localhost:${PORT}/postgres"

cmd_start() {
  local bin; bin="$(find_pg_bin)"
  if [ ! -d "$DATADIR/base" ]; then
    log "initdb -> $DATADIR (locale $LOCALE)"
    LC_ALL="$LOCALE" "$bin/initdb" -D "$DATADIR" --locale="$LOCALE" --encoding=UTF8 -U postgres >/dev/null
  fi
  if "$bin/pg_isready" -h localhost -p "$PORT" -U postgres >/dev/null 2>&1; then
    log "already up on :$PORT"
  else
    log "starting on :$PORT (logs: $DATADIR/server.log)"
    "$bin/pg_ctl" -D "$DATADIR" -o "-p $PORT -k /tmp" -l "$DATADIR/server.log" start >/dev/null
    for _ in $(seq 1 20); do
      "$bin/pg_isready" -h localhost -p "$PORT" -U postgres >/dev/null 2>&1 && break
      sleep 0.3
    done
  fi
  log "ready — exporting KANTAQ_TEST_POSTGRES_URL"
  echo "export KANTAQ_TEST_POSTGRES_URL=$URL"
}

cmd_stop() {
  local bin; bin="$(find_pg_bin)"
  if "$bin/pg_isready" -h localhost -p "$PORT" -U postgres >/dev/null 2>&1; then
    log "stopping :$PORT"
    "$bin/pg_ctl" -D "$DATADIR" stop -m fast >/dev/null || true
  fi
  if [ "${1:-}" = "--purge" ]; then log "purging $DATADIR"; rm -rf "$DATADIR"; fi
}

case "${1:-}" in
  start) cmd_start ;;
  stop)  cmd_stop "${2:-}" ;;
  url)   echo "$URL" ;;
  *) echo "usage: $0 {start|stop [--purge]|url}" >&2; exit 2 ;;
esac
