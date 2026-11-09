"""Generic decisions preserve structure while skills validate business parameters."""

import pytest
from pydantic import ValidationError

from server.agent.schemas import Decision, Target


@pytest.mark.parametrize(
    "step",
    [
        {"action": ""},
        {"action": "  "},
        {"action": "expand", "parameters": []},
        {"action": "expand", "parameters": {}, "skill": "expand-cache"},
        {"kind": "expand", "desired_replicas": 2},
    ],
)
def test_invalid_step_structure_is_rejected(step):
    with pytest.raises(ValidationError):
        Decision(status="action", reason="test", steps=[step])


@pytest.mark.parametrize("action", ["expand", "preload", "shrink", "custom-operation"])
def test_new_actions_need_no_core_schema_change(action):
    decision = Decision(
        status="action", reason="test", steps=[{"action": action, "parameters": {"custom": [1, 2]}}]
    )

    assert decision.steps[0].action == action
    assert decision.steps[0].parameters == {"custom": [1, 2]}


@pytest.mark.parametrize("status", ["action", "noop", "insufficient_data"])
def test_decision_status_must_match_steps(status):
    steps = [] if status == "action" else [{"action": "custom", "parameters": {}}]
    with pytest.raises(ValidationError, match="steps are required only"):
        Decision(status=status, reason="test", steps=steps)


def test_runtime_resource_type_must_be_supplied(target):
    with pytest.raises(ValidationError, match="runtime_plural"):
        Target.model_validate(target.model_dump(exclude={"runtime_plural"}))
