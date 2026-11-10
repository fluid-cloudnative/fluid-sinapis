# bin/_lib.sh -- service registry and start/stop implementation shared by start/stop/restart
# shellcheck shell=bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONBIN="$REPO_ROOT/.venv/bin/python"
RUN_DIR="$REPO_ROOT/run"
LOG_DIR="$REPO_ROOT/logs"
WEB_PORT="${WEB_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
# Listen on all interfaces by default: the typical deployment runs the services
# on one machine and fronts them with a reverse proxy on another, which needs a
# non-loopback bind. The HTTP API is protected by a shared API key (SINAPIS_API_KEY,
# checked by the backend itself), but the key is sent in the clear over plain HTTP
# and the ports are now reachable from any network the host is attached to — put a
# TLS-terminating reverse proxy in front for anything beyond a trusted LAN, or set
# WEB_HOST to a specific address (e.g. the private IP) to narrow the exposure.
WEB_HOST="${WEB_HOST:-0.0.0.0}"
export WEB_PORT FRONTEND_PORT WEB_HOST

mkdir -p "$RUN_DIR" "$LOG_DIR"

# Service list (start order = dependency order; stop runs in reverse)
ALL_SERVICES=(collector backend frontend agent)

pid_file() { echo "$RUN_DIR/$1.pid"; }
log_file() { echo "$LOG_DIR/$1.log"; }

is_running() {
    local pid
    pid="$(cat "$(pid_file "$1")" 2>/dev/null)" || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# Parse arguments: none means all services; otherwise validate each service name
resolve_services() {
    [ $# -eq 0 ] && return 0
    local name
    for name in "$@"; do
        case "$name" in
            collector|backend|frontend|agent) ;;
            *)
                echo "Unknown service: $name (available: collector | backend | frontend | agent)" >&2
                return 1
                ;;
        esac
    done
    ALL_SERVICES=("$@")
}

# Emit the start command for a service (as the CMD array)
service_command() {
    case "$1" in
        collector) CMD=("$PYTHONBIN" server/collectors/run_collectors.py) ;;
        backend)   CMD=("$PYTHONBIN" -m server.controller.web_controller) ;;
        agent)     CMD=("$PYTHONBIN" -m server.agent.run_agents) ;;
        frontend)  CMD=(bash -c "cd '$REPO_ROOT/web' && npm run dev -- --host '$WEB_HOST' --port '$FRONTEND_PORT'") ;;
    esac
}

start_service() {
    local name="$1"
    if is_running "$name"; then
        echo "• $name is already running (pid $(cat "$(pid_file "$name")")), skipping"
        return 0
    fi

    # Pre-start checks
    # Note: case matches the FIRST matching pattern, so service-specific checks
    # must come before any branch that also lists that service.
    case "$name" in
        backend)
            if [ ! -x "$PYTHONBIN" ]; then
                echo "✗ $name was not started: $PYTHONBIN not found (install the dependencies first, see docs/en/user-guide.md)" >&2
                return 1
            fi
            # The backend refuses to start without a key anyway; fail here with the
            # actionable hint up front so the cause is visible before the log tail.
            if [ -z "${SINAPIS_API_KEY:-}" ] && [ -z "${SINAPIS_AUTH_DISABLED:-}" ]; then
                echo "✗ backend was not started: export SINAPIS_API_KEY=<key from \`openssl rand -hex 32\`> first (persisted in your environment, not in git; see docs/en/user-guide.md section on the API key)" >&2
                return 1
            fi
            ;;
        collector|agent)
            if [ ! -x "$PYTHONBIN" ]; then
                echo "✗ $name was not started: $PYTHONBIN not found (install the dependencies first, see docs/en/user-guide.md)" >&2
                return 1
            fi
            ;;
        frontend)
            # Node.js is optional: with a built web/dist the backend serves the page and
            # no frontend process is needed, so a missing dev setup only skips (and the
            # page answers 503 with build instructions until dist exists).
            if [ ! -d "$REPO_ROOT/web/node_modules" ]; then
                if [ -f "$REPO_ROOT/web/dist/index.html" ]; then
                    echo "• frontend needs no separate process: the backend serves web/dist (http://$WEB_HOST:$WEB_PORT/)"
                else
                    echo "• frontend skipped: no web/node_modules and no web/dist build; the page will answer 503 until you run: cd web && npm ci && npm run build"
                fi
                return 0
            fi
            if ! command -v npm >/dev/null 2>&1; then
                echo "✗ frontend was not started: npm not found (Node.js is required, see docs/en/user-guide.md)" >&2
                return 1
            fi
            ;;
    esac

    service_command "$name"
    # Note: the process must be backgrounded in the current shell before reading $!;
    # wrapping it in a ( ... & echo $! ) subshell captures the intermediate fork's pid
    # and stop would then fail to find the real process.
    pushd "$REPO_ROOT" >/dev/null || return 1
    if [ "$name" = "agent" ]; then
        # The agent process already writes its stage logs to logs/agent.log (rotated);
        # stdout (the structured report JSON) appends to the same file and the stderr
        # console copy is kept separately.
        nohup "${CMD[@]}" >> "$LOG_DIR/agent.log" 2>> "$LOG_DIR/agent-console.log" < /dev/null &
    else
        nohup "${CMD[@]}" >> "$(log_file "$name")" 2>&1 < /dev/null &
    fi
    local pid=$!
    popd >/dev/null
    echo "$pid" > "$(pid_file "$name")"
    # A bare `sleep 1` races interpreter startup: a Python service can spend >1s
    # importing (fastapi/uvicorn) before a config error kills it, so the probe
    # finds a live pid and reports "started" for a process that dies right after.
    # Poll instead: fail fast the moment the process exits (within ~3s), and
    # treat "still alive after the window" as started.
    local _i _alive=1
    for _i in $(seq 1 15); do
        if ! is_running "$name"; then
            _alive=0
            break
        fi
        sleep 0.2
    done
    if [ "$_alive" = 1 ]; then
        if [ "$name" = "frontend" ]; then
            # Vite auto-increments its port when the requested one is taken, and
            # a foreign process may even be squatting on the port — either way
            # the healthy-looking process serves somewhere else. Verify that the
            # process listening on the expected port is this checkout's server.
            local _try _lpid=""
            local _probe_host="$WEB_HOST"
            [ "$_probe_host" = "0.0.0.0" ] && _probe_host=127.0.0.1
            for _try in $(seq 1 10); do
                _lpid="$(ss -tlnpH "sport = :$FRONTEND_PORT" 2>/dev/null | sed -n -E 's/.*pid=([0-9]+).*/\1/p' | head -n 1)"
                [ -n "$_lpid" ] && break
                sleep 0.5
            done
            if [ -z "$_lpid" ] || [ "$(readlink "/proc/$_lpid/cwd" 2>/dev/null)" != "$REPO_ROOT/web" ]; then
                echo "✗ $name was not started: the process listening on $FRONTEND_PORT is not this checkout's dev server (port taken by a stray process — vite drifted to a different port), tail of the log:" >&2
                tail -n 5 "$(log_file "$name")" >&2 2>/dev/null
                _kill_tree TERM "$pid"
                rm -f "$(pid_file "$name")"
                return 1
            fi
        fi
        echo "✓ $name started (pid $pid), log: logs/$name.log"
        return 0
    fi
    echo "✗ $name failed to start, tail of the log:" >&2
    tail -n 5 "$(log_file "$name")" >&2 2>/dev/null
    rm -f "$(pid_file "$name")"
    return 1
}

# Kill a process and every descendant (TERM first, escalate to KILL on request).
# Killing only the wrapper pid leaks the real server: `npm run dev` spawns a
# bash -> node chain, and an orphaned node keeps serving its port for as long
# as the process lives — repeated start/stop cycles then pile up stray servers.
_kill_tree() {
    local sig="$1" root="$2" kid
    for kid in $(pgrep -P "$root" 2>/dev/null); do
        _kill_tree "$sig" "$kid"
    done
    kill "$sig" "$root" 2>/dev/null
}

stop_service() {
    local name="$1" pid i
    if ! is_running "$name"; then
        rm -f "$(pid_file "$name")"
        echo "• $name is not running"
        return 0
    fi
    pid="$(cat "$(pid_file "$name")")"
    _kill_tree TERM "$pid"
    for i in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        _kill_tree KILL "$pid"
        echo "✓ $name was force-stopped (pid $pid)"
    else
        echo "✓ $name stopped (pid $pid)"
    fi
    rm -f "$(pid_file "$name")"
}
