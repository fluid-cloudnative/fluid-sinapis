# user_skills: user-defined cache strategies

Each subdirectory here defines one custom strategy (`SKILL.md` + `scripts/decide.py`),
loaded automatically every round:

- **SKILL.md**: strategy semantics (trigger conditions, output operation, relation to
  builtin strategies), injected into the planner and the validator;
- **scripts/decide.py**: deterministic decision script — reads the metric-snapshot JSON
  from stdin and prints one JSON line to stdout:
  `{"action": "expand"|"preload"|"shrink"|"evict"|null, "parameters": {...}, "reason": "≤300 chars"}`.
  `action=null` means no opinion this round; non-zero exit / timeout / bad JSON are
  recorded and never block the loop.

## Conflict rule

When a custom strategy and a builtin strategy disagree on the **same operation** (e.g.
expand), **the custom strategy prevails**. The rule binds the planner (must follow) and
the validator (must verify). `action` must be one of the executable action catalogue types
with parameters per its conventions; a custom action with no catalogue tool is not an
error, but the step stays unmapped, execution stops there (later steps are skipped), and
the round reports `not_executed`.

## Conventions

- The directory name is the strategy name. Strategy discovery skips directories starting
  with an underscore, such as `_template`;
- Rescanned every round in loop mode: added or modified strategies take effect on the next
  round, no restart needed;
- Scripts run with the agent process's permissions, 60 s timeout each; use only the
  standard library or dependencies already in `.venv`.

Quick start: copy `_template/` to your strategy name, then edit SKILL.md and decide.py.

## Add a troubleshooting case

1. Set `on_demand_cases: true` under `agent` in `server/config.yml` and restart the agent.
2. Create `_cases/user.scale-up-pending.json` under `skills.user_dir` (this directory by default).
   Copy the example below and replace the details with your own maintenance notes.
3. On the next round, the planner can read the case when its `when` description matches.

```json
{
  "id": "user.scale-up-pending",
  "when": "The Runtime shows two desired workers, but only one is Ready.",
  "evidence": [
    "The scale request was accepted.",
    "The reported cache capacity was still 5 GiB."
  ],
  "handling": "Deferred preload and checked again after the second worker became Ready.",
  "lesson": "Verify the reported cache capacity before planning a preload."
}
```

Use the same ID in the JSON and filename. Write `when` on one line and list the observed
facts in `evidence`. Remove the file to retire the case.
