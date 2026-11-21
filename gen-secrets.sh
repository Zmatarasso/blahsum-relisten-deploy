#!/usr/bin/env bash
# Generates db.env / api.env with random secrets (never echoed).
# Safe to re-run: existing files are kept unless --force is given.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [[ -f db.env && -f api.env && "${1:-}" != "--force" ]]; then
  echo "db.env/api.env already exist (use --force to rotate)"; exit 0
fi
rnd() { openssl rand -hex 32; }
PW=$(rnd)
URL="postgresql://relisten:${PW}@db:5432/relisten_db"
printf 'POSTGRES_PASSWORD=%s\n' "$PW" > db.env
{
  printf 'DATABASE_URL=%s\n' "$URL"
  printf 'PGBOUNCER_DATABASE_URL=%s\n' "$URL"
  printf 'PANIC_KEY=%s\n' "$(rnd)"
  printf 'RELISTEN_ADMIN__USERNAME=admin\n'
  printf 'RELISTEN_ADMIN__PASSWORD=%s\n' "$(rnd)"
} > api.env
chmod 600 db.env api.env
echo "wrote db.env and api.env (mode 600). If the database already exists,"
echo "apply the new password with: ./rotate-db-password.sh"
