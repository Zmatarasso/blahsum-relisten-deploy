#!/usr/bin/env bash
# Sets the relisten DB role's password to the one in db.env (never echoed).
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./db.env; set +a
docker compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U relisten -d relisten_db \
  -v pw="$POSTGRES_PASSWORD" <<'SQL'
SET password_encryption = 'scram-sha-256';
ALTER ROLE relisten WITH PASSWORD :'pw';
SQL
echo "relisten role password updated"
