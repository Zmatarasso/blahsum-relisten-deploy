#!/usr/bin/env bash
# blahsum-relisten — first-time setup on the host (Linux / TrueNAS SCALE).
# Clones the API and web forks as siblings of this repo, on the right branches.
#
# Usage:
#   git clone https://github.com/Zmatarasso/blahsum-relisten-deploy.git
#   cd blahsum-relisten-deploy
#   ./bootstrap.sh
#   docker compose up -d db redis pgbouncer adminer
#   docker compose up -d --build api audio

set -euo pipefail

cd "$(dirname "$0")"

API_REPO="https://github.com/Zmatarasso/blahsum-relisten-api.git"
API_BRANCH="blahsum/empty-db-bootstrap"   # has the empty-DB patch
WEB_REPO="https://github.com/Zmatarasso/blahsum-relisten.git"
WEB_BRANCH="blahsum/dockerfile-zfs-fix"   # has the pnpm-throttle Dockerfile patch

clone_or_update () {
  local dir="$1" repo="$2" branch="$3"
  if [[ -d "$dir/.git" ]]; then
    echo "==> updating $dir"
    git -C "$dir" fetch --quiet origin "$branch"
    git -C "$dir" checkout --quiet "$branch"
    git -C "$dir" pull --quiet --ff-only
  else
    echo "==> cloning $dir from $repo ($branch)"
    git clone --branch "$branch" "$repo" "$dir"
  fi
}

clone_or_update RelistenApi  "$API_REPO" "$API_BRANCH"
clone_or_update relisten-web "$WEB_REPO" "$WEB_BRANCH"

mkdir -p audio data/postgres data/redis

echo
echo "Done. Next:"
echo "  docker compose up -d db redis pgbouncer adminer"
echo "  docker compose up -d --build api audio"
echo "  # check API: curl http://localhost:3823/api-docs"
