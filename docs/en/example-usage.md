# Example (User-System Simulator)

> 中文文档：[Example 使用文档（用户系统模拟器）](../zh/example-usage.md)
>
> Related docs: [User Guide](user-guide.md) · [Custom Strategies & Metrics](custom-strategies-and-metrics.md)

`examples/user_system_simulator.py` is a simulator that depends **only on the Python standard
library**. It plays the role of a cloud MaaS inference system and pushes model-file access
events plus a download-bandwidth sample to the web console. Two pushes are enough to walk the
agent through the whole chain — preload → expand → evict → shrink — because everything after
the expansion follows from the cache state alone.

---

## 1. How It Works

The simulator **pushes once and exits**; it does not loop. One push does two things:

1. `POST /api/user-system/events`: pushes a fixed number of `model_file_access` events for
   each file of the scenario (file path + access time + file size; the count comes from
   `SCENARIO_ACCESSES`: 50 for preload, 60 for expand). The size is the file's real HDFS
   size, so the agent's capacity arithmetic is exact;
2. `POST /api/user-system/metrics`: pushes one random (20–120 MiB/s)
   `model_download_bandwidth` sample.

Which files are pushed is decided by `--scenario`, which is **required**: `preload` or
`expand`. The access count is a fixed burst per scenario: it only marks a file as hot, it
does not model a request rate. The `source` identity defaults to
`hdfs-simulator` — it labels pushed metrics in Prometheus and is informational for events
(event counters are stored under the fixed grouping key `source=user_system`).

## 2. Prerequisites

- Start all services as described in the [Quick Start](quick-start.md).
- Prepare the data: run `python examples/data_generate.py`. It creates six files in the current
  directory, at the real HDFS sizes the simulator reports:

  ```
  big_model_104.zip  3152012255  (2.94GiB)
  big_model_105.zip  2643485774  (2.46GiB)
  big_model_106.zip  2830948396  (2.64GiB)
  big_model_107.zip  2841337723  (2.65GiB)
  big_model_108.zip  1985894795  (1.85GiB)
  big_model_109.zip  1671222590  (1.56GiB)
  ```

  Upload the six files to HDFS and create a Fluid Dataset mounted at `/models/data`;
  continue only once the Dataset is mounted. Verify before starting:

  ```bash
  # Dataset and Runtime exist and are bound (adjust -n and names to your target section)
  kubectl -n <namespace> get dataset <dataset>
  kubectl -n <namespace> get alluxioruntime <runtime>
  ```

  If a later step misbehaves, these are the shortest checks:

  ```bash
  kubectl -n <namespace> get dataload            # DataLoads created by the agent (preload)
  kubectl -n <namespace> get alluxioruntime <runtime> -o jsonpath='{.status.replicas}{"\n"}'
                                                 # replica count after expand/shrink
  kubectl -n <namespace> get pods -l fluid.io/dataset=<namespace>-<dataset>
                                                 # worker pods Ready?
  ```
- Capacity assumption: **one worker, 5 GiB of cache**. 108+109 are 3.41 GiB (68 % used) and
  fit; adding 104+105 needs 8.80 GiB and does not.

## 3. Scenarios

| Scenario | Push behavior | Expected agent decision |
|---|---|---|
| `preload` | One push: 50 accesses each for 108 and 109 | Preload 108+109 — 3.41 GiB, 68 % of the 5 GiB worker, so no expansion and nothing above the 70 % evict line yet |
| `expand` | One push: 60 accesses each for 104 and 105, on top of the cached 108/109 | 8.80 GiB against one 5 GiB worker → expand to `desired_replicas=2`; the 104/105 preload follows once the new capacity is Ready |
| *(no third push)* | — | With 8.80 GiB of 10 GiB used (88 %) the next rounds cross the 70 % evict line and free the cold files (about two or three of them); what remains is 1.56–2.94 GiB (16–29 %), below the shrink line, so the surplus worker is dropped |

So a full demo is: run `preload`, wait until 108/109 are actually cached, run `expand`, then
just keep the agent running — eviction and shrink arrive on their own in the following rounds.
If earlier experiments left the counters skewed, run `python examples/clear_prometheus.py --yes`
before starting.

## 4. Command-Line Options

| Option | Default | Description |
|---|---|---|
| `--url` | `http://127.0.0.1:8000` | Web console address |
| `--source` | `hdfs-simulator` | Pusher identity (labels pushed metrics; informational for events) |
| `--scenario` | — (required) | `preload` or `expand` |

The access counts are the module constant `SCENARIO_ACCESSES` (50 for preload, 60 for
expand) in the script — change them there if you want a different burst; the two scenarios
are independent, so the expand pair arrives hotter than the preload pair.

## 5. Running

```bash
# 1. Start the web console (if it is not already running)
.venv/bin/python -m server.controller.web_controller

# 2. Heat 108/109 and let the agent preload them
.venv/bin/python examples/user_system_simulator.py --scenario preload

# 3. Once 108/109 are cached, heat 104/105 on top of them
.venv/bin/python examples/user_system_simulator.py --scenario expand
```

Example output (the script prints English):

```
config loaded: 1 metric, 1 event type (reload_error=None)
scenario=preload: 2 files x 50 accesses = 100 events, 3.41GiB (one push)
  pushed 50x big_model_108.zip (1.85GiB)
  pushed 50x big_model_109.zip (1.56GiB)
  expected: preload 108+109 = 3.41GiB of 5.00GiB per worker, no expansion
```

A rejected push or a network error (refused connection, timeout) prints the reason and exits
non-zero.

## 6. Watching the Autonomy Loop

With the pushes done:

1. **Web console**: open `http://127.0.0.1:8000` and watch `/api/overview`, `/api/fluid`
   and `/api/k8s` change;
2. **Agent logs**: start the main agent (`.venv/bin/python -m server.agent.run_agents`, or
   `--once` for a single round) and watch one full cycle: read metrics → user-strategy
   verdicts (if any) → LLM planning → LLM review → skill script execution → report;
3. **PromQL to confirm ingestion**:

   ```promql
   rate(model_file_access_count[5m])          # access rate per file
   model_file_last_access_time                # last access time per file
   model_file_size_bytes                      # file sizes
   model_download_bandwidth                   # bandwidth
   ```

4. **Executed actions**: with `execution.dry_run: false` you will see DataLoad CRDs
   (preload), Runtime replica changes (expand and later shrink) and eviction runs in the
   cluster; with `dry_run: true` the manifests are only printed in the agent logs.

## 7. FAQ

**Q1: The simulator immediately fails with HTTPError 4xx?**
Check `GET /api/user-system/metrics/schema`: the event/metric is not in the configured list
(`unknown_event` / `unknown_metric`) or the `user_system` config failed to load (503). Fix
config.yml, wait ~1 s for the hot reload, and retry.

**Q2: Pushes succeed but the agent does nothing?**
The agent decides on a period (default 10 s, `agent.interval_seconds` in
`server/config.yml`) and needs observation windows to accumulate data; confirm the agent
process is running and Prometheus can evaluate
`rate(model_file_access_count[5m])`. Use `--once` to trigger a round immediately.

**Q3: `preload` does not preload anything?**
108+109 are 3.41 GiB and only fit while the cache really has ~5 GiB free — if 104/105 (or
anything else) are already cached from an earlier run, clear the Fluid cache first. Also check
that `execution.allowed_path_prefixes` covers `/models/data` (paths outside the allowlist are
rejected before a script runs) and that `execution.dry_run` is `false` if you expect real
DataLoad CRDs.

**Q4: `expand` never triggers?**
Two preconditions: (1) 108/109 are already cached, so run `preload` first and let it finish —
the expansion only becomes necessary once the worker is 68 % full and a 5.40 GiB pair arrives;
(2) the replica count is still below the ceiling (`policies.expand.max_workers`, default 2) —
at the ceiling the agent answers a full cache with evict-cache, not with expansion. Allow a
few rounds: the 104/105 preload only appears once the new worker is Ready.

**Q5: Can the simulator and a real business system push simultaneously?**
Yes. Pushed metrics keep their per-source grouping, so different `--source` values stay
separate; event counters are merged into one global counter per file, so simultaneous pushers
cannot double-count. If you change labels for an existing source, DELETE its old group first
(see User Guide FAQ Q3).

**Q6: Why are there no `evict` and `shrink` scenarios?**
They need no push of their own. `expand` leaves the two-worker cache at 8.80 GiB of 10 GiB
(88 %), which is above the evict line, so the next rounds free cold files by themselves; once
usage falls below the shrink line the surplus worker goes away. Keep the agent running and
watch the logs — that is the whole point of the design.
