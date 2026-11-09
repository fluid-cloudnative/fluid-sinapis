#!/usr/bin/env bash
# Restart system services (equivalent to stop + start)
# Usage: bin/restart.sh [collector|backend|frontend|agent ...]
#        no arguments = restart everything
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

"$DIR/stop.sh" "$@"
"$DIR/start.sh" "$@"
