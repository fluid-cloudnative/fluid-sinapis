# User Guide (Download · Install · Configure · Start)

> 中文文档：[用户使用文档](../zh/user-guide.md)
>
> Related docs: [Custom Strategies & Metrics](custom-strategies-and-metrics.md) · [Example (User-System Simulator)](example-usage.md)

Fluid-Sinapis-Agent is an AI-agent-based elastic cache management system for Fluid. It closes
the loop of **scheduled multi-source metric collection → LLM diagnosis → strategy planning →
LLM review → Fluid execution → result recording**. This guide walks first-time users through
the full path from cloning the repo to a working end-to-end deployment.

---

## Contents

1. [System Components](#1-system-components)
2. [Prerequisites](#2-prerequisites)
3. [Get the Code](#3-get-the-code)
4. [Install Python Dependencies](#4-install-python-dependencies)
5. [Prepare the Infrastructure](#5-prepare-the-infrastructure)
6. [Configuration Reference](#6-configuration-reference)
7. [Starting the System](#7-starting-the-system)
8. [Verifying the Deployment](#8-verifying-the-deployment)
9. [Recommendations for the First Run](#9-recommendations-for-the-first-run)
10. [Development acceptance checklist](#10-development-acceptance-checklist)
11. [FAQ / Troubleshooting](#11-faq--troubleshooting)

---

## 1. System Components

The system consists of four independently startable processes that share one config file,
`server/config.yml` (each process reads only its own sections):

| Process | Entry point | Responsibility |
|---|---|---|
| Collectors | `server/collectors/run_collectors.py` | Periodically pull Fluid and K8s metrics and push them to Pushgateway |
| HTTP backend (web console) | `server/controller/web_controller.py` | Serves metric query APIs and the frontend; hosts the user-system push API |
| Main agent | `server/agent/run_agents.py` | Loop: read time-series metrics → LLM planning → independent LLM review → run Fluid skill scripts → record structured results |
| User-system simulator (optional) | `examples/user_system_simulator.py` | Simulates a real business system pushing access events/metrics to the HTTP backend for local testing |

```
User system/simulator ──POST──▶ HTTP backend ──▶ Pushgateway ◀── Collectors (Fluid/K8s)
                                   │                  │
                                   ▼                  ▼
                        Prometheus (read :9090, scrapes Pushgateway :9091)
                                   │
                                   ▼
                        Main agent (read → plan → review → execute on Fluid)
```

---

## 2. Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | **>= 3.12** (declared in `pyproject.toml`) | Runs all four processes |
| uv (recommended) | latest | Create the virtualenv and install dependencies; plain `python3.12 -m venv` also works |
| Kubernetes cluster | >= 1.30 | Hosts Fluid and the cache workloads |
| Fluid | >= 1.0.8 | Cache engine (Alluxio / JuiceFS, etc.) |
| Prometheus | 2.x | Metric read channel; agent and web console query it via PromQL |
| Pushgateway | latest | Metric write channel; collectors and external pushes both write here |
| LLM | OpenAI-compatible API | One model per role (planner / validator) — GLM, DeepSeek, SinapisAI, etc. |
| Node.js | >= 20.19 (optional) | Only needed to build the web console frontend (`web/dist`) from source |

Scope: this system does **not** replace the Fluid cache engine, Prometheus collection, or K8s
scheduling, and it does not handle storage failures or network partitions.

---

## 3. Get the Code

```bash
git clone https://github.com/fluid-cloudnative/fluid-sinapis.git
cd fluid-sinapis
```

Layout at a glance:

```
fluid-sinapis/
├── server/
│   ├── config.yml            # the single config file (see section 6)
│   ├── collectors/           # collectors (run_collectors.py)
│   ├── controller/           # web console + user-system push API
│   ├── agent/                # agent loop, skills, executor
│   │   └── skills/
│   │       ├── official/     # built-in strategy skills (planning/review)
│   │       ├── review_skills/# skills exclusive to the review agent
│   │       ├── fluid_skills/ # operational skills (with executable scripts)
│   │       └── user_skills/  # user-defined strategies (with a _template)
│   └── requirements.txt      # pinned dependency list
├── examples/                  # user-system simulator
├── docs/                     # documentation (zh / en)
└── tests/
```

---

## 4. Install Python Dependencies

Recommended: uv (install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`):

```bash
uv venv --python 3.12
uv pip install -r server/requirements.txt -p .venv/bin/python
```

Or plain venv:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
```

Notes:

- **Use one canonical flow**: `server/requirements.txt` is the pinned dependency
  list exported from `pyproject.toml` (`uv export --format requirements-txt >
  server/requirements.txt` regenerates it after changing `pyproject.toml`). Install
  from it as shown above. `uv.lock` is kept in sync — verify with `uv lock --check`;
- Always start the processes with `.venv/bin/python`; the system Python (often 3.10) cannot
  install the dependencies;
- Developers additionally need `pytest` and `ruff`: `uv pip install pytest ruff`, then run
  tests with `.venv/bin/python -m pytest` (see the [development acceptance
  checklist](#10-development-acceptance-checklist)).

---

## 5. Prepare the Infrastructure

### 5.1 Kubernetes + Fluid

1. The kubeconfig must contain the context referenced by the config:

   ```bash
   kubectl config get-contexts        # find the context name
   kubectl get namespace              # confirm the namespace exists
   ```

2. A Fluid `Dataset` and `Runtime` (e.g. AlluxioRuntime) must exist in the target namespace
   with names matching `target.dataset` / `target.runtime_name`:

   ```bash
   kubectl -n <namespace> get dataset
   kubectl -n <namespace> get alluxioruntime
   ```

### 5.2 Pushgateway and Prometheus

Pushgateway (write channel, default :9091):

```bash
# Persistence is optional but recommended so recent samples survive restarts
pushgateway --web.listen-address=:9091 --persistence.file=~/pushgateway.data
```

Prometheus (read channel, default :9090), scrape the Pushgateway in `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: pushgateway
    honor_labels: true          # keep pushed labels such as "source" — required
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:9091']
```

> `honor_labels: true` matters: without it the pushed `source` label gets renamed and
> grouping by the pushing system breaks.

### 5.3 Model API Keys

Model keys live **only in environment variables**, never in the config file:

```bash
export LLM_API_KEY=<your key>    # matches the ${LLM_API_KEY} placeholder
```

Put the export in `~/.bashrc` or your startup script so the agent process sees it.

---

## 6. Configuration Reference

All configuration lives in `server/config.yml`; each process reads only its own sections:

| Section | Read by | Purpose |
|---|---|---|
| `target` | collectors / web / agent (shared) | **Single source** for kube context, namespace, dataset, runtime |
| `k8s` | collectors / web | Collection semantics (node role, free-space mode, timeout) |
| `prometheus` | collectors / web / agent (shared) | `pushgateway_url` (write) + `query_url` (read) |
| `collectors` | collectors | Collection interval |
| `agent` | agent | Loop interval, enable/disable second review |
| `model` / `models` | agent | Model connections (shared defaults + per-role overrides) |
| `skills` | agent | Locations of the four skill directories |
| `execution` | agent | Execution limits (path allowlist, timeouts, dry-run switch) |
| `policies` | agent | Strategy parameters (replica increment, preload limits, etc.) |
| `user_system` | web console (ingestion) / agent (observations) | Allowed external metrics/events — **the only hot-reloaded section** |

### 6.1 Key Settings

```yaml
target:
  kube_context: kubernetes-admin@cluster   # from kubectl config get-contexts
  namespace: jgd-fluid
  dataset: nju-model-hdfs                  # Dataset name
  runtime_name: nju-model-hdfs             # empty = follow dataset
  runtime_plural: alluxioruntimes          # plural of the Runtime CRD

prometheus:
  pushgateway_url: http://localhost:9091   # write
  query_url: http://localhost:9090         # read

agent:
  interval_seconds: 10                     # decision loop period (matches config.yml)
  validation_enabled: true                 # default in config.yml; false = skip the second LLM review

model:                                     # shared defaults
  api_key: ${LLM_API_KEY}              # env-var placeholder only
  base_url: https://open.bigmodel.cn/api/paas/v4
  name: glm-5.3-flash
  temperature: 0
  timeout_seconds: 300

models:                                    # per-role overrides (planner/validator only)
  planner:  { name: glm-5.3-flash }
  validator: { name: glm-5.3-flash }

execution:
  allowed_path_prefixes: [/models]         # UFS path prefixes allowed for preload/evict
  timeout_seconds: 660                     # per-script timeout; timeout = unknown result, no retry. Must exceed wait_timeout_seconds
  wait: true                                # wait for async operations to really finish (on by default)
  wait_timeout_seconds: 600                 # how long one script may poll, covered by the timeout above
  dry_run: false                            # dry-run: print manifests without touching the cluster

policies:
  expand:  { replica_increment: 1 }
  preload: { max_files: 3, min_file_size_bytes: 1024 }
```

Conventions and rules:

- **Priority: environment variables > `config.yml` > code defaults**;
- Write each semantic value in exactly one place (cluster info in `target`, Prometheus
  addresses in `prometheus`) to avoid drift;
- Old keys (`k8s.namespace`, `k8s.kube_context`, `fluid.*`, `metrics.prometheus_url`,
  `prometheus.prometheus_query_url`) have been merged into the new sections; leftovers fail
  **at startup** with a message pointing to the new location;
- Except for the `user_system` section (hot-reloaded), processes must be restarted after a
  config change.

### 6.2 Environment Variables

| Variable | Config it overrides | Default |
|---|---|---|
| `SINAPIS_CONFIG` | config file path (collectors/web) | `server/config.yml` |
| `KUBE_CONTEXT` / `KUBE_NS` | `target.kube_context` / `target.namespace` | see config.yml |
| `FLUID_DATASET` / `FLUID_RUNTIME` / `FLUID_RUNTIME_PLURAL` | `target.*` | see config.yml |
| `K8S_NODE_ROLE` / `K8S_FREE_MODE` / `K8S_TIMEOUT_SECONDS` | `k8s.*` | worker / request / 10 |
| `PUSHGATEWAY_URL` / `PROMETHEUS_URL` / `PROMETHEUS_TIMEOUT_SECONDS` | `prometheus.*` | :9091 / :9090 / 10 |
| `COLLECTOR_INTERVAL_SECONDS` | `collectors.interval_seconds` | 10 |
| `LLM_API_KEY` etc. | `${...}` placeholders in `model.api_key` | unset → startup error |
| `WEB_HOST` / `WEB_PORT` | web listen address | 127.0.0.1 / 8000 everywhere (code and `bin/start.sh`); set `WEB_HOST=0.0.0.0` only for deliberate LAN exposure — the API has no authentication, see the security note in section 7 |
| `WEB_CACHE_TTL_SECONDS` / `WEB_CORS_ORIGINS` | web cache & CORS | 5 / localhost |
| `WEB_CACHED_FILES_SCAN_PATH` / `WEB_CACHED_FILES_TTL_SECONDS` | Alluxio scan path / result cache time for the cached-files table | /models / 30 s |
| `SINAPIS_USER_METRICS_CONFIG` | separate config file for the user-system metric/event definitions (web console) | defaults to the `SINAPIS_CONFIG` file |
| `AGENT_LOG_FILE` | agent stage-log file path; set to `off` to disable file output | `<repo>/logs/agent.log` |

---

## 7. Starting the System

> **⚠ Security note — the HTTP API has no authentication.** The backend serves the
> read-only metric APIs *and* writable push endpoints (`POST /api/user-system/metrics`
> and `POST /api/user-system/events`). Keep `WEB_HOST` on `127.0.0.1` (the default of
> both the code and the start scripts), never expose the port directly to a public
> network, and if LAN access is required put an authenticating reverse proxy or an
> API gateway in front first.

### Option 1: one-shot scripts (recommended)

The repo ships `bin/start.sh` / `bin/stop.sh` / `bin/restart.sh` managing four services:
**collector**, **backend** (HTTP API + web page + push endpoint), **frontend** (vite dev
server, only for development), and **agent** (the main service).

```bash
bin/start.sh                 # start all (order: collector → backend → frontend → agent)
bin/start.sh backend agent   # start only the named services
bin/stop.sh                  # stop all
bin/stop.sh backend          # stop only the named services
bin/restart.sh               # restart all
```

Notes:

- PIDs go to `run/<service>.pid`, logs to `logs/<service>.log`; re-running start skips
  services that are already up;
- The agent fails fast at startup when a `${...}` placeholder in `config.yml` (e.g.
  `LLM_API_KEY`) references an unset environment variable;
- The backend listens on `127.0.0.1:8000` by default — override with
  `WEB_PORT=9000 bin/start.sh backend`; the frontend port defaults to 5173
  (`FRONTEND_PORT`);
- **API data always comes from real cluster collection** (there is no demo mode); when one
  section fails to collect it returns `error` with the reason, leaving other sections intact;
- frontend: `bin/start.sh frontend` runs the vite dev server only when
  `web/node_modules` is present; otherwise it skips (exit 0) — with `web/dist` built the
  backend serves the page directly, and with neither the page answers 503 with build
  instructions until you run `cd web && npm ci && npm run build`.

### Agent stage logs (logs/agent.log)

The agent process dual-writes the key stage logs of each decision round to `logs/agent.log`
(rotating, 10MB × 5; override the path or set `off` via `AGENT_LOG_FILE`): round start/end
(with approved and execution status, duration), metric reads, user-strategy verdicts, LLM
planning and review (with rationale), every Fluid skill execution step, and failure
tracebacks. The structured report JSON (stdout) is appended to the same file by
`bin/start.sh`; the stderr console copy goes to `logs/agent-console.log`. To follow along:

```bash
tail -f logs/agent.log
```

### Option 2: manual commands

For custom arguments or systemd units, run from the repository root in order:

```bash
# 1. Collectors: periodically push Fluid/K8s metrics
.venv/bin/python server/collectors/run_collectors.py

# 2. HTTP backend (web console + push API), listens on 127.0.0.1:8000
.venv/bin/python -m server.controller.web_controller

# 3. (Optional, for testing) user-system simulator (--scenario is required: preload or expand)
.venv/bin/python examples/user_system_simulator.py --scenario preload

# 4. Main agent: periodic decision loop
.venv/bin/python -m server.agent.run_agents
```

Further notes:

- **The web console must be started as a module** (`python -m ...`); launching
  `server/controller/web_controller.py` directly fails on imports. The collector script
  handles its own paths and works either way;
- The agent accepts `--config <path>` for an alternative config file and `--once` for a
  single decision round (recommended while debugging):
  ```bash
  .venv/bin/python -m server.agent.run_agents --once
  ```

---

## 8. Verifying the Deployment

```bash
# Web backend alive
curl http://127.0.0.1:8000/api/healthz

# Metric overview (populated once collectors have pushed)
curl http://127.0.0.1:8000/api/overview

# Follow the agent stage logs (rounds / metrics / planning / review / execution)
tail -f logs/agent.log

# Allowed external metrics/events (includes hot-reload status)
curl http://127.0.0.1:8000/api/user-system/metrics/schema

# K8s / Fluid sections
curl http://127.0.0.1:8000/api/k8s
curl http://127.0.0.1:8000/api/fluid
```

On the Prometheus side:

```promql
up == 1                                       # the pushgateway job should be 1
{job="fluid_agent:fluid"}                     # metrics pushed by the collectors
```

Agent logs follow the order
`read metrics → LLM planning → decision → LLM review → script execution → report`.
Every executed step carries an `exit_code` and the script's JSON output.

---

## 9. Recommendations for the First Run

1. **Dry-run first**: set `execution.dry_run: true` for one or two rounds and check the
   planned manifests (printed, not applied) before switching back to `false`;
2. **Single round first**: use `run_agents --once` to validate the whole chain
   (metrics readable → model reachable → plan executable) before enabling the loop;
3. **Keep the path allowlist tight**: `execution.allowed_path_prefixes` should cover the
   actual UFS paths of `target.dataset`; never leave it empty (= unrestricted);
4. **Keep the review on**: `agent.validation_enabled: true` lets a second LLM independently
   review the plan against status and operation rules — the key guard against model mistakes;
5. **Model timeouts**: large-context reasoning can exceed 2 minutes; keep
   `model.timeout_seconds` at 300 or above, or pick a non-thinking model for faster rounds.

---

## 10. Development acceptance checklist

Run all of these from the repository root before considering a change done — the same
checks run in CI (`.github/workflows/ci.yml`):

```bash
# Python: lint and full test suite
python -m ruff check .
python -m pytest tests -q

# Web console: reproducible install from the committed lock file, then build
cd web && npm ci && npm run build && cd ..
```

---

## 11. FAQ / Troubleshooting

**Q1: Startup fails with “config `k8s.namespace` has been merged into `target.namespace`”?**
Leftover old keys. Move the values into the new sections as the error message says, delete
the old keys, and restart.

**Q2: Startup fails with “`model.api_key` references unset environment variable
LLM_API_KEY”?**
The `${...}` placeholder has no matching exported variable. `export LLM_API_KEY=...`
before starting the agent.

**Q3: Pushing returns 400 (duplicate series)?**
After changing labels/grouping, stale groups in the Pushgateway collide with new ones
(duplicate series in the same job). DELETE the old group first, e.g.
`curl -X DELETE http://localhost:9091/metrics/job/<job>/<grouping-key>`.

**Q4: SSL EOF / connection failures to the K8s API while `https_proxy` is set?**
kubernetes client 34.x reads `https_proxy` into its connection config and clears `no_proxy`,
so internal cluster addresses (e.g. `*.local`) get hijacked by the proxy. Unset the proxy
variables or make sure the in-code direct-connection bypass is effective.

**Q5: Web page returns 503 “frontend build not found”?**
`web/dist` is missing: `cd web && npm ci && npm run build`. Ignore if you only use the API.

**Q6: `uv sync` fails on the lock file?**
If the workspace `uv.lock` is damaged (e.g. entries missing the source field), restore it
with `git checkout uv.lock`, or simply install from `server/requirements.txt` as in section 4.

**Q7: How to switch Prometheus / Pushgateway addresses?**
Edit the `prometheus` section of `server/config.yml` (or set `PROMETHEUS_URL` /
`PUSHGATEWAY_URL`); all three sides share it. Restart the collectors and the agent after.
