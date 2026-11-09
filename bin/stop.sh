#!/usr/bin/env bash
# Stop system services: agent, web frontend, HTTP backend, collector (reverse of start)
# Usage: bin/stop.sh [collector|backend|frontend|agent ...]
#        no arguments = stop everything
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

resolve_services "$@" || exit 1

rc=0
echo "== Stopping Fluid-Sinapis-Agent =="
for name in $(printf '%s\n' "${ALL_SERVICES[@]}" | tac); do
    stop_service "$name" || rc=1
done
exit $rc
