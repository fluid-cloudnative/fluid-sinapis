#!/usr/bin/env bash
# Start system services: collector, HTTP backend, web frontend, agent
# Usage: bin/start.sh [collector|backend|frontend|agent ...]
#        no arguments = start everything
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

resolve_services "$@" || exit 1

rc=0
echo "== Starting Fluid-Sinapis-Agent (backend :$WEB_PORT, frontend :$FRONTEND_PORT) =="
for name in "${ALL_SERVICES[@]}"; do
    start_service "$name" || rc=1
done
exit $rc
