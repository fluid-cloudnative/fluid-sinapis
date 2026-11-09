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

- The directory name is the strategy name; **directories starting with an underscore
  (like `_template`) are never loaded**;
- Rescanned every round in loop mode: added or modified strategies take effect on the next
  round, no restart needed;
- Scripts run with the agent process's permissions, 60 s timeout each; use only the
  standard library or dependencies already in `.venv`.

Quick start: copy `_template/` to your strategy name, then edit SKILL.md and decide.py.
