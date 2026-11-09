# bin/_lib.sh -- service registry and start/stop implementation shared by start/stop/restart
# shellcheck shell=bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONBIN="$REPO_ROOT/.venv/bin/python"
RUN_DIR="$REPO_ROOT/run"
LOG_DIR="$REPO_ROOT/logs"
WEB_PORT="${WEB_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
# 0.0.0.0 exposes the console on all interfaces (LAN access); override back to
# 127.0.0.1 when that is not wanted.
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
    case "$name" in
        collector|backend|agent)
            if [ ! -x "$PYTHONBIN" ]; then
                echo "✗ $name was not started: $PYTHONBIN not found (install the dependencies first, see docs/en/user-guide.md)" >&2
                return 1
            fi
            if [ "$name" = "agent" ] && [ -z "${LLM_API_KEY:-}" ]; then
                echo "✗ agent was not started: export LLM_API_KEY=<model key> first (the \${LLM_API_KEY} placeholder in config.yml needs it)" >&2
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
                    echo "• frontend skipped: no web/node_modules and no web/dist build; the page will answer 503 until you run: cd web && npm install && npm run build"
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
    sleep 1
    if is_running "$name"; then
        echo "✓ $name started (pid $pid), log: logs/$name.log"
        return 0
    fi
    echo "✗ $name failed to start, tail of the log:" >&2
    tail -n 5 "$(log_file "$name")" >&2 2>/dev/null
    rm -f "$(pid_file "$name")"
    return 1
}

stop_service() {
    local name="$1" pid i
    if ! is_running "$name"; then
        rm -f "$(pid_file "$name")"
        echo "• $name is not running"
        return 0
    fi
    pid="$(cat "$(pid_file "$name")")"
    kill -TERM "$pid" 2>/dev/null
    for i in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null
        echo "✓ $name was force-stopped (pid $pid)"
    else
        echo "✓ $name stopped (pid $pid)"
    fi
    rm -f "$(pid_file "$name")"
}
