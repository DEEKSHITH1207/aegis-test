#!/usr/bin/env bash
# Pull-based deployment: run on the server by deploy/aegis-autodeploy.timer.
#
# Fetches AUTODEPLOY_BRANCH (default: main) and, when it has moved, fast-forwards
# the checkout and rebuilds with docker-compose.prod.yml. Nothing reaches in from
# outside: the server only makes outgoing connections to GitHub, with its
# read-only deploy key.
#
# A checkout that cannot fast-forward (local commits, a force push) is left as
# it is and the run fails, so a deploy never discards anything on the server.
set -euo pipefail

BRANCH="${AUTODEPLOY_BRANCH:-main}"
COMPOSE=(docker compose -f docker-compose.prod.yml)

cd "$(dirname "$0")/.."

# Two runs at once would build twice; a run that finds the lock taken skips.
exec 9>/tmp/aegis-autodeploy.lock
if ! flock -n 9; then
    echo "another deploy is running; skipping"
    exit 0
fi

git fetch --quiet origin "$BRANCH"

if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
    git checkout --quiet "$BRANCH"
fi

current="$(git rev-parse HEAD)"
target="$(git rev-parse "origin/$BRANCH")"

if [ "$current" = "$target" ] && [ "${1:-}" != "--force" ]; then
    exit 0
fi

echo "deploying $BRANCH: ${current:0:7} -> ${target:0:7}"
git merge --quiet --ff-only "origin/$BRANCH"

# migrate runs before backend and worker start, as on a manual deploy.
"${COMPOSE[@]}" up -d --build --remove-orphans

# Each build leaves the previous images behind; a small disk fills up.
docker image prune -f >/dev/null

echo "deployed ${target:0:7}"
