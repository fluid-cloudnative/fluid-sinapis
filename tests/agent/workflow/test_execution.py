"""Verify the review gate and the hand-off to the execution agent."""

import json
from pathlib import Path

import pytest

from server.agent.execution import ExecutionReport
from server.agent.workflow import AgentWorkflow

SKILLS = Path(__file__).resolve().parents[3] / "server/agent/skills/official"


class RecordingExecutor:
    """Stand in for PlanExecutor so the workflow tests stay free of subprocesses."""

    def __init__(self, report: ExecutionReport | None = None) -> None:
        self.report = report or ExecutionReport(status="completed", reason="Executed")
        self.calls: list[object] = []

    def execute(self, decision):  # noqa: ANN001, ANN201 - mirrors PlanExecutor
        self.calls.append(decision)
        return self.report


def test_approved_action_plan_is_handed_to_the_executor(model_factory, decision):
    planner = model_factory(("Decision", decision))
    validator = model_factory(("PlanValidation", {"approved": True, "reason": "Consistent"}))
    executor = RecordingExecutor()
    context = {"target": {"namespace": "test"}}

    result = AgentWorkflow(planner, validator, executor=executor, skills_dir=SKILLS).run(
        "Prepare the cache", context
    )

    assert result.validation.approved
    assert result.execution is not None
    assert result.execution == executor.report
    assert len(executor.calls) == 1
    assert executor.calls[0].model_dump() == decision


@pytest.mark.parametrize(
    "status,approved", [("action", False), ("noop", True), ("insufficient_data", True)]
)
def test_rejected_or_non_action_plan_never_reaches_execution(
    model_factory, decision, status, approved
):
    decision = {
        **decision,
        "status": status,
        "steps": decision["steps"] if status == "action" else [],
    }
    planner = model_factory(("Decision", decision))
    validator = model_factory(("PlanValidation", {"approved": approved, "reason": "Reviewed"}))
    executor = RecordingExecutor()

    result = AgentWorkflow(planner, validator, executor=executor, skills_dir=SKILLS).run(
        "Check cache"
    )

    assert result.validation.approved == approved
    assert result.execution is None
    assert not executor.calls


def test_workflow_without_executor_keeps_execution_empty(model_factory, decision):
    planner = model_factory(("Decision", decision))
    validator = model_factory(("PlanValidation", {"approved": True, "reason": "Consistent"}))

    result = AgentWorkflow(planner, validator, skills_dir=SKILLS).run("Prepare the cache")

    assert result.validation.approved
    assert result.execution is None


def test_execution_outcome_is_reported_without_replanning(model_factory, decision):
    planner = model_factory(("Decision", decision))
    validator = model_factory(("PlanValidation", {"approved": True, "reason": "Consistent"}))
    report = ExecutionReport(status="failed", reason="Step 0 failed")
    executor = RecordingExecutor(report)

    result = AgentWorkflow(planner, validator, executor=executor, skills_dir=SKILLS).run(
        "Prepare the cache"
    )

    assert result.execution.status == "failed"
    assert result.model_dump_json()  # the result is serialisable, so the entry point can print it
    assert len(planner.requests) == 1 and len(validator.requests) == 1


def test_rounds_reuse_agents_without_reusing_conversations(model_factory, decision):
    planner = model_factory(("Decision", decision), ("Decision", decision))
    approved = ("PlanValidation", {"approved": True, "reason": "Consistent"})
    validator = model_factory(approved, approved)
    executor = RecordingExecutor()
    workflow = AgentWorkflow(planner, validator, executor=executor, skills_dir=SKILLS)

    workflow.run("First round", {"round": 1})
    workflow.run("Second round", {"round": 2})

    for model in (planner, validator):
        assert len(model.requests) == 2
        assert json.loads(model.requests[1][-1].content)["context"] == {"round": 2}
    assert len(executor.calls) == 2
