# Quick Start

> 中文文档：[快速开始](../zh/quick-start.md)
>
> This page keeps the shortest path only. For full installation/configuration details and
> the FAQ, see the [User Guide](user-guide.md); related docs:
> [Custom Strategies & Metrics](custom-strategies-and-metrics.md) · [Example (Simulator)](example-usage.md)

## Prerequisites

Check before starting (details in the [User Guide](user-guide.md#5-prepare-the-infrastructure)):

- [ ] Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or plain venv)
- [ ] A reachable Kubernetes cluster with the target context in your kubeconfig, Fluid
      installed (Dataset + Runtime created)
- [ ] Pushgateway (:9091) and Prometheus (:9090, scraping the Pushgateway with
      `honor_labels: true`) running
- [ ] An API key for an OpenAI-compatible LLM (GLM / DeepSeek / SinapisAI, etc.)

## 1. Get the Code and Install

```bash
git clone https://github.com/fluid-cloudnative/fluid-sinapis.git
cd fluid-sinapis
uv venv --python 3.12
uv pip install -r server/requirements.txt -p .venv/bin/python
```

Optional, only if you want the web console page: build the frontend once (the API
works without it; without `web/dist` the page answers 503):

```bash
cd web && npm ci && npm run build && cd ..
```

## 2. Minimal Configuration

Edit the `target` section of `server/config.yml` with your cluster info:

```yaml
target:
  kube_context: kubernetes-admin@cluster   # from kubectl config get-contexts
  namespace: jgd-fluid                     # namespace of the Dataset/Runtime
  dataset: nju-model-hdfs                  # your Dataset name
  runtime_name: nju-model-hdfs             # empty = follow dataset
```

Export the model key and the API key:

```bash
export LLM_API_KEY=<your key>
export SINAPIS_API_KEY=$(openssl rand -hex 32)   # generate ONCE, then persist (see below)
```

> The API key protects every HTTP endpoint (`Authorization: Bearer`). Generate it
> once and persist it in your deployment environment (shell profile, systemd
> `EnvironmentFile`, …) — it must stay the same across restarts and must never be
> committed to git. Pushers and the web console send the same key; details in the
> [user guide](user-guide.md#api-key-authentication).

## 3. Start

Use the bundled scripts (manual per-process commands are in the
[User Guide](user-guide.md#7-starting-the-system)):

```bash
bin/start.sh          # start all: collector → backend → frontend → agent
```

- PIDs go to `run/`, logs to `logs/`; `bin/stop.sh` stops, `bin/restart.sh` restarts;
- The backend refuses to start without `SINAPIS_API_KEY` and every endpoint requires
  `Authorization: Bearer <key>` (clearly opt out with `SINAPIS_AUTH_DISABLED=true`
  for a keyless local run only);
- The backend listens on `127.0.0.1:8000` and its API data **always comes from real
  cluster collection**. Keep `127.0.0.1` unless you deliberately expose it: the key
  travels over plain HTTP, so LAN/public exposure needs a TLS-terminating reverse
  proxy (`WEB_HOST=0.0.0.0` opens it to all interfaces);
- The agent fails fast at startup when the `${...}` placeholder in `config.yml`
  (e.g. `LLM_API_KEY`) references an unset environment variable, and says so explicitly;
- For a first run, validate a single round before enabling the loop:
  ```bash
  .venv/bin/python -m server.agent.run_agents --once     # manual single round
  bin/restart.sh agent                                   # or restart the agent service
  ```

> For a first integration, set `execution.dry_run` to `true` in `server/config.yml` for a
> rehearsal round (manifests are printed, nothing is applied), then switch back to `false`.

## 4. Verify

```bash
curl http://127.0.0.1:8000/api/healthz          # web backend alive
curl http://127.0.0.1:8000/api/overview         # live-collected metric overview
tail -f logs/agent.log                          # agent stage logs (rounds/metrics/planning/review/execution)
```

The agent log should show one full cycle: `read metrics → LLM planning → review → execute → report`.

## 5. Demo the Autonomy Loop (Optional)

Let the simulator play the business system and trigger preload, then expansion:

```bash
.venv/bin/python examples/user_system_simulator.py --scenario preload
```

See the [Example doc](example-usage.md) for details.

## No Kubernetes Cluster?

There is no demo mode: without a cluster each section of the API returns `error` with the
underlying reason, and the page renders empty. To see the UI with live-looking data you
need a real cluster.

## What's Next

- [User Guide](user-guide.md): configuration reference, environment variables, FAQ
- [Custom Strategies & Metrics](custom-strategies-and-metrics.md): ingest business data, write custom strategies
- [Example](example-usage.md): simulator scenarios and options
