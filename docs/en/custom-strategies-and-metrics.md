# Custom Strategies & Metrics

> 中文文档：[自定义策略与指标使用文档](../zh/custom-strategies-and-metrics.md)
>
> Related docs: [User Guide](user-guide.md) · [Example (User-System Simulator)](example-usage.md)

This document describes the two extension points of the system:

1. **Custom metric ingestion** — pipe your business data into the metric pipeline (the
   `user_system` section of `config.yml` + the HTTP push API);
2. **Custom cache strategies** — inject your own cache decision logic (the
   `server/agent/skills/user_skills/` directory: SKILL.md + a decision script).

They can be used independently or together: business metrics are pushed in → your strategy
script reads those metrics and produces a verdict → the planner and reviewer follow it.

---

## Contents

- [Part 1: Custom Metric Ingestion (Passive Push)](#part-1-custom-metric-ingestion-passive-push)
  - [Two Ingestion Methods](#two-ingestion-methods)
  - [Configuring the user_system Section](#configuring-the-user_system-section)
  - [Push API](#push-api)
  - [Validation Rules and Error Codes](#validation-rules-and-error-codes)
  - [Event-Derived Metrics and PromQL Queries](#event-derived-metrics-and-promql-queries)
  - [How the Agent Reads Them](#how-the-agent-reads-them)
- [Part 2: Custom Cache Strategies](#part-2-custom-cache-strategies)
  - [Layout and Loading](#layout-and-loading)
  - [SKILL.md: Strategy Semantics](#skillmd-strategy-semantics)
  - [decide.py: Decision Script Contract](#decidepy-decision-script-contract)
  - [Conflict Rule](#conflict-rule)
  - [A Complete Example](#a-complete-example)
  - [Manual Debugging](#manual-debugging)

---

# Part 1: Custom Metric Ingestion (Passive Push)

Business data that cannot be scraped actively (model download bandwidth, file access logs,
etc.) is **pushed via POST** by the user system to the web console. After server-side
validation it lands in Pushgateway and is queried by the agent through Prometheus.

## Two Ingestion Methods

| | Method 1: push metrics directly | Method 2: push events |
|---|---|---|
| Best for | The user system computes statistics itself | It can only report "an access happened"; rates are derived server-side |
| Endpoint | `POST /api/user-system/metrics` | `POST /api/user-system/events` |
| Payload | Metric name + value (+ labels) | Event name + dimension labels + numeric fields |
| Stored as | What you push is what gets stored (gauge/counter) | Auto-accumulated counter + numeric fields stored as gauges (latest value) |
| Rate statistics | Compute it yourself before pushing | Not needed — query `rate(counter[5m])` in Prometheus |

## Configuring the user_system Section

Declare the allowed metrics and events in the `user_system` section of `server/config.yml`:

```yaml
user_system:
  # Method 1: directly pushed metrics
  metrics:
    - name: model_download_bandwidth      # must satisfy Prometheus naming rules
      help: model download bandwidth (bytes/sec)   # required, non-empty
      # type: gauge                       # gauge (default) or counter
      # labels: [source_id]               # label keys required when pushing (optional)

  # Method 2: events
  events:
    - name: model_file_access             # event name used by the pushing side
      help: model file access event
      labels: [file]                      # dimensions the event must carry
      counter: model_file_access_count    # counter name maintained by the server
      gauges:                             # numeric field → gauge mapping
        - field: access_time              # field name inside the event
          metric: model_file_last_access_time   # gauge metric name to store
          help: last access time of the model file (Unix ms)
        - field: size_bytes
          metric: model_file_size_bytes
          help: model file size in bytes
```

**Hot reload**: changes to this section take effect **without restarting** the web console —
the server watches the file's mtime and reloads within about a second. If the new config
fails to parse, the old config keeps serving (visible as `reload_error` in the schema API).

Naming rules:

- Metric names (`name` / `counter` / `metric`): `^[a-zA-Z_:][a-zA-Z0-9_:]*$` (Prometheus spec);
- Label keys and event field names: `^[a-zA-Z_][a-zA-Z0-9_]*$`;
- Duplicate derived metric names (two events deriving the same name) make the **agent fail
  at startup** — keep names unique.

## Push API

### Method 1: `POST /api/user-system/metrics`

```bash
curl -X POST http://127.0.0.1:8000/api/user-system/metrics \
  -H "Authorization: Bearer $SINAPIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "model-service-01",
    "metrics": [
      {"name": "model_download_bandwidth", "value": 1048576.0}
    ]
  }'
```

- Every endpoint requires the API key (see the
  [API key section](user-guide.md#api-key-authentication) in the user guide);
- `source`: identity of the pushing system, used as the Prometheus grouping key;
- `metrics[]`: `name` must be in the configured list; `value` must be a finite number;
  metrics declaring `labels` in the config must carry those keys (extra keys are preserved).

### Method 2: `POST /api/user-system/events`

```bash
curl -X POST http://127.0.0.1:8000/api/user-system/events \
  -H "Authorization: Bearer $SINAPIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "model-service-01",
    "events": [
      {
        "name": "model_file_access",
        "labels": {"file": "/models/a.safetensors"},
        "fields": {"access_time": 1792982400000, "size_bytes": 1073741824}
      }
    ]
  }'
```

- `labels` must contain every configured dimension; `fields` must contain every field
  referenced by `gauges`, all finite numbers;
- The `counter` metric is maintained by the server — **pushers never send it**.

### Allowed list: `GET /api/user-system/metrics/schema`

Returns the effective metric/event configuration (integrations can generate push code from
it) plus the hot-reload status (`config.metric_count` / `config.event_count` /
`config.reload_error`).

## Validation Rules and Error Codes

Both endpoints use **partial-success semantics**: one rejected item does not affect the rest
of the batch; the response reports each item individually.

| status | reason_code | Meaning |
|---|---|---|
| accepted | — | Received and stored |
| rejected | `unknown_metric` / `unknown_event` | Not in the configured list |
| rejected | `missing_labels` | Required label missing |
| rejected | `missing_fields` | Fields referenced by gauges missing |
| rejected | `invalid_value` | Field is not a finite number (NaN/Inf, etc.) |

A failed write of the whole batch returns **502**; if the `user_system` config failed to
load and no previous config is available, the endpoints return **503**.

## Event-Derived Metrics and PromQL Queries

Each event automatically maintains two kinds of metrics:

- **counter**: occurrence count accumulated per (event, labels). Counters resetting to zero
  on process restart is fine — `rate()` handles resets automatically;
- **gauges**: numeric fields stored as gauges, keeping the latest value.

Derived metrics go to a separate pushgateway job (`fluid_agent:user_system_events`) so they
never overwrite directly pushed metrics (job `fluid_agent:user_system`).

Example queries:

```promql
# Access rate per file (accesses/sec)
rate(model_file_access_count[5m])

# Top 3 hottest files in the last 5 minutes
topk(3, sum by (file) (rate(model_file_access_count[5m])))

# File size and last access time (latest values)
model_file_size_bytes
model_file_last_access_time

# Directly pushed bandwidth metric
model_download_bandwidth
```

## How the Agent Reads Them

- The agent reads the latest sample within the last 5 minutes from Prometheus through the
  `read_user_system_metrics` tool;
- The tool is attached to the agent **only when** the `user_system` section is non-empty;
  the metric-name list is validated at agent startup (bad names or duplicate derived names
  fail at startup);
- These metrics flow into both the planner's observation context and the `observations`
  input of custom strategy scripts (see Part 2).

---

# Part 2: Custom Cache Strategies

The built-in strategies (`expand-cache` / `preload-cache` / `shrink-cache`) are prompt-based
skills in `server/agent/skills/official/`. Your custom strategies live under
`server/agent/skills/user_skills/` and are **rescanned every loop round — no restart needed**.

## Layout and Loading

```
server/agent/skills/user_skills/
├── README.md                 # directory conventions
├── _template/                # official template (underscore prefix → not loaded)
│   ├── SKILL.md              # strategy semantics (for planner / reviewer)
│   ├── README.md             # human reference (not sent to the model)
│   └── scripts/
│       └── decide.py         # deterministic decision script
└── my-strategy/              # ← your strategy: copy _template and rename
    ├── SKILL.md
    └── scripts/decide.py
```

Loading rules:

- **The directory name is the strategy name**; directories starting with an underscore
  (like `_template`) are never loaded;
- **Rescanned every loop round**: added or modified strategies take effect on the next
  round, no restart; in `--once` mode simply run again;
- A missing directory is treated as "no custom strategies", not an error;
- The two parts of each strategy play distinct roles:
  - `SKILL.md` is injected into the **planner and reviewer** contexts (strategy semantics);
  - `scripts/decide.py` is executed by the framework every round (deterministic verdict);
    its verdict enters planning and review as the highest-priority evidence.

## SKILL.md: Strategy Semantics

Follow the skill format: YAML frontmatter (`name` + `description`) + Markdown body.
The body must state clearly (it is read by the model — the clearer the semantics, the more
stable planning and review become):

```markdown
---
name: my-strategy
description: One sentence describing what the strategy does.
---

# <Strategy Name>

## Strategy Semantics (for planner / reviewer)

- Trigger: which observations/metrics meeting which conditions should trigger action;
- Output action: one of `expand` / `preload` / `shrink` / `evict`, with parameters following
  the executable action catalogue;
- Conflict rule: when this strategy and a built-in strategy disagree on the same action,
  this strategy prevails;
- Boundaries: when to explicitly do nothing (action=null).
```

## decide.py: Decision Script Contract

Every round the framework feeds an **observation snapshot** to the script via stdin and
reads **one line** of JSON from stdout:

```
stdin:  {"observations": [{"tool": "...", "result": {...}}, ...],
         "context": {"target": {...}, "policies": {...}}}

stdout: {"action": "expand"|"preload"|"shrink"|"evict"|null,
         "parameters": {...}, "reason": "<=300 chars"}
```

Details:

| Item | Description |
|---|---|
| `observations` | Same sources as the planner: one entry each for `read_fluid_metrics` / `read_k8s_metrics` / `read_user_system_metrics`; `result.metrics` is a list of `{"name", "labels", "value"}` (including the latest values of `user_system` custom metrics) |
| `context` | `target` (cluster/dataset/runtime) and `policies` (the policies section of config.yml), identical to what the planner receives |
| `action` | Must be a type in the executable action catalogue; `null` means "no opinion this round" |
| `parameters` | `expand`/`shrink` → `{"desired_replicas": N}` (N is the target total); `preload` → `{"paths": [...]}` (paths must come from real observations and hit the `execution.allowed_path_prefixes` allowlist); `evict` → `{"path": "/dir", "target_bytes": N}` or `{"count": N}` (exactly one of `target_bytes`/`count`; `path` is the scanned directory — evict is approximate LRU and cannot target a single file) |
| `reason` | ≤300 characters; enters the planning rationale and logs |
| Fault tolerance | Non-zero exit / timeout (60 s) / malformed JSON are recorded and **never block the loop** |
| Dependencies | The script runs with the agent process's permissions; use the standard library or dependencies already in `.venv` |

## Conflict Rule

When a custom strategy and a built-in strategy disagree on the **same action** (e.g. expand),
**the custom strategy prevails**. The rule binds both the planner (must follow) and the
reviewer (must verify). A custom action with no catalogue tool is not an error, but it is
not free either: the step stays unmapped, execution stops there (later steps are skipped),
and the round reports `not_executed`.

Strategy parameters (`context.policies`) are set in `server/config.yml`:

```yaml
policies:
  expand:  { replica_increment: 1 }              # default replicas added per expansion
  preload: { max_files: 3, min_file_size_bytes: 1024 }   # per-round preload limits
```

## A Complete Example

This decision script suggests expanding when the download bandwidth exceeds 100 MiB/s
(based on `_template`):

```python
#!/usr/bin/env python3
import json, sys

BANDWIDTH_THRESHOLD_BYTES = 100 * 1024 * 1024

def flatten_metrics(observations):
    """Flatten all observation sources into {(metric name, labels): value}."""
    metrics = {}
    for observation in observations:
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        for metric in result.get("metrics", []):
            key = (str(metric.get("name")),
                   json.dumps(metric.get("labels", {}), sort_keys=True))
            if isinstance(metric.get("value"), (int, float)):
                metrics[key] = float(metric["value"])
    return metrics

def main() -> int:
    payload = json.load(sys.stdin)
    metrics = flatten_metrics(payload.get("observations", []))
    bandwidth = max((v for (name, _), v in metrics.items()
                     if name == "model_download_bandwidth"), default=0.0)
    if bandwidth > BANDWIDTH_THRESHOLD_BYTES:
        verdict = {"action": "expand",   # planner computes desired_replicas from policies
                   "parameters": {},
                   "reason": f"bandwidth {bandwidth:.0f}B/s exceeds threshold; expand"}
    else:
        verdict = {"action": None, "reason": "bandwidth below threshold"}
    print(json.dumps(verdict, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Write matching trigger conditions in SKILL.md. Steps to use:

1. `cp -r server/agent/skills/user_skills/_template server/agent/skills/user_skills/bandwidth-expand`
2. Edit the semantics in `SKILL.md` and the rules in `decide.py`;
3. It takes effect on the next loop round; in `--once` mode, run again.

## Manual Debugging

```bash
# Smoke test with empty input (expect one line of JSON with action=null)
echo '{"observations": [], "context": {}}' \
  | .venv/bin/python server/agent/skills/user_skills/bandwidth-expand/scripts/decide.py

# Debug with real metrics: copy the observations part from agent logs into obs.json
echo "{\"observations\": $(cat obs.json), \"context\": {}}" \
  | .venv/bin/python server/agent/skills/user_skills/bandwidth-expand/scripts/decide.py
```

The verdict appears in agent logs as `[user strategy] <name> verdict: action=<...>` and in
the planner input under `custom_strategy_verdicts`.
