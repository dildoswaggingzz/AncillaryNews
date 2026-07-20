#!/usr/bin/env bash
# Restarts every docker-compose service that bind-mounts ./shared into
# /app/shared, so a local `shared/*.py` code change actually takes effect.
#
# **Why this exists.** `docker-compose.yml` bind-mounts `./shared` (host)
# over `/app/shared` (container) on the `api`, `ingestor`, `crawler`, and
# `orchestrator` services -- this makes edits under `shared/` show up on
# disk inside the container immediately, with no image rebuild. But each of
# those four is a long-running Python process that already `import`ed
# `shared.*` modules at startup; Python caches imported modules in
# `sys.modules`; an on-disk change to an already-imported module does
# *nothing* to a process that's still running -- only a process restart
# re-imports it. This bit us for real: after a `git checkout`, the
# `ingestor` container was restarted but `api` was not, and `api` went on
# rejecting valid dataset names against a stale, pre-checkout validation
# list -- a confusing error with no obvious connection to a checkout that
# had happened in a completely different container. Restart every
# ./shared-mounting service together, every time, rather than guessing which
# ones matter for a given change.
#
# **Service list derivation.** Rather than hardcoding the four names above
# (which would silently rot the day a fifth service starts mounting
# ./shared), this asks `docker compose config` itself which services mount a
# volume whose *container-side target* is `/app/shared` -- see
# docker-compose.yml's `volumes:` entries under `api`/`ingestor`/`crawler`/
# `orchestrator`. If that derivation ever fails (e.g. `jq` unavailable, or
# `docker compose config` errors), this falls back to the hardcoded list
# below with a loud warning -- update FALLBACK_SERVICES if that list ever
# changes, since it's the one place left that can rot silently.
#
# Usage:
#   scripts/restart-shared-services.sh
#
# Requires `docker compose` and `jq` for the derived path (both already
# expected in this repo's dev environment -- see DEPLOYMENT.md); works with
# just `docker compose` if `jq` is missing, via the fallback list.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Kept in sync manually with docker-compose.yml's ./shared:/app/shared
# mounts -- only used if the `docker compose config` + `jq` derivation below
# fails. See the comment above for why the derived path is preferred.
FALLBACK_SERVICES=(api ingestor crawler orchestrator)

services=""
if command -v jq >/dev/null 2>&1; then
    services="$(docker compose config --format json 2>/dev/null \
        | jq -r '.services | to_entries[]
            | select((.value.volumes // []) | any(.target == "/app/shared"))
            | .key' 2>/dev/null || true)"
fi

if [ -z "$services" ]; then
    echo "warning: could not derive the ./shared-mounting service list from" >&2
    echo "docker-compose.yml (needs 'docker compose config' + 'jq'); falling" >&2
    echo "back to the hardcoded list: ${FALLBACK_SERVICES[*]}" >&2
    services="$(printf '%s\n' "${FALLBACK_SERVICES[@]}")"
fi

echo "Restarting ./shared-mounting service(s): $(echo "$services" | tr '\n' ' ')"
# shellcheck disable=SC2086
docker compose restart $services
