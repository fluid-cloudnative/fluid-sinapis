# example-custom-strategy: human reference manual

For execution semantics see [SKILL.md](SKILL.md). This file is a manual debugging
reference.

## Run the decision script manually

```bash
echo '{"observations": [], "context": {}}' \
  | .venv/bin/python server/agent/skills/user_skills/_template/scripts/decide.py
# expected output: {"action": null, "parameters": {}, "reason": "custom condition not met"}
```

To debug with a real metric snapshot: copy the observations part from the agent logs into
obs.json, then:

```bash
echo "{\"observations\": $(cat obs.json), \"context\": {}}" \
  | .venv/bin/python .../scripts/decide.py
```

## Inputs available to the script

- `observations`: identical to what the planner receives — one entry per observation
  source (`read_fluid_metrics` / `read_k8s_metrics` / `read_user_system_metrics`);
  `result.metrics` holds the latest values of the config.yml `user_system` custom metrics;
- `context`: `{"target": {...}, "policies": {...}}` (the same as what the planner gets).

## Steps to use

1. Copy `_template/` to `<your-strategy-name>/` (do not start the name with an underscore);
2. Edit the strategy semantics in `SKILL.md` and the decision rules / output operation in
   `decide.py`;
3. In loop mode it takes effect on the next round; in `--once` mode, run again.
