#!/usr/bin/env bash
# Create/refresh a dedicated PostgreSQL 16 test database for the window tool.
#
# This script never touches production databases.  It starts a throwaway
# cluster under .runtime/ (ignored by git) if necessary, creates a dedicated
# database, and loads fixtures/seed.sql.  Re-running it reloads the fixture
# table from scratch.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
RUNTIME="${ROOT}/.runtime"
PGDATA="${RUNTIME}/pgdata"
SOCKET_DIR="${WINDOW_LAB_SOCKET_DIR:-/tmp}"
PORT="${WINDOW_LAB_PORT:-5433}"
DBNAME="${WINDOW_LAB_DB:-window_lab}"
PSQL=("${PGBIN}/psql" -h "${SOCKET_DIR}" -p "${PORT}")

mkdir -p "${RUNTIME}"

if [[ ! -f "${PGDATA}/PG_VERSION" ]]; then
  "${PGBIN}/initdb" -D "${PGDATA}" --auth-local=trust --auth-host=scram-sha-256 >/dev/null
fi

if ! "${PGBIN}/pg_ctl" -D "${PGDATA}" status >/dev/null 2>&1; then
  # A socket directory inside the repo may not support socket permissions on
  # some overlay filesystems, so default to /tmp.
  "${PGBIN}/pg_ctl" -D "${PGDATA}" -l "${RUNTIME}/postgres.log" \
    -o "-c listen_addresses='' -p ${PORT} -k ${SOCKET_DIR}" start >/dev/null
fi

if ! "${PSQL[@]}" -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
  echo "cannot reach PostgreSQL on ${SOCKET_DIR}:${PORT}" >&2
  exit 1
fi

if ! "${PSQL[@]}" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='${DBNAME}'" | grep -q 1; then
  "${PGBIN}/createdb" -h "${SOCKET_DIR}" -p "${PORT}" "${DBNAME}"
fi

"${PSQL[@]}" -d "${DBNAME}" -c 'DROP TABLE IF EXISTS window_events' >/dev/null
"${PSQL[@]}" -d "${DBNAME}" -f "${ROOT}/fixtures/seed.sql" >/dev/null

cat <<EOF
Database ready.
  export WINDOW_LAB_DSN='host=${SOCKET_DIR} port=${PORT} dbname=${DBNAME}'
EOF
