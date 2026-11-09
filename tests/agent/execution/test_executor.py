"""Verify catalogue mapping, execution order, and stop-without-retry behaviour."""

from typing import Any

import pytest

from server.agent.execution import ExecutionReport, PlanExecutor, SkillRun
from server.agent.schemas import Decision


class FakeRunner:
    """Queue script outcomes and record what the executor asked to run."""

    def __init__(self, *results: SkillRun) -> None:
        self.queued = list(results)
        self.calls: list[tuple[str, dict[str, Any], object]] = []

    def run(self, spec, arguments, *, target):  # noqa: ANN001, ANN201 - mirrors SkillRunner
        self.calls.append((spec.name, dict(arguments), target))
        if self.queued:
            return self.queued.pop(0)
        return SkillRun(status="ok", payload={"status": "ok"})


def make_decision(*steps: tuple[str, dict[str, Any]]) -> Decision:
    """Build an action decision from (action, parameters) pairs."""
    return Decision.model_validate(
        {
            "status": "action",
            "reason": "Add capacity and preload files that fit the available space",
            "steps": [
                {"action": action, "parameters": parameters} for action, parameters in steps
            ],
        }
    )


@pytest.fixture
def approved():
    """A reviewed two-step plan: scale out, then preload two files."""
    return make_decision(
        ("expand", {"desired_replicas": 2}),
        ("preload", {"paths": ["/models/a.bin", "/models/b.bin"]}),
    )


def test_each_mapped_step_runs_once_in_plan_order(approved, target):
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(approved)

    assert report.status == "completed"
    assert [outcome.status for outcome in report.outcomes] == ["ok", "ok"]
    assert [call[0] for call in runner.calls] == ["expand_cache", "preload_cache"]
    assert runner.calls[0][1] == {"desired_replicas": 2}
    assert runner.calls[1][1]["paths"] == ["/models/a.bin", "/models/b.bin"]
    assert all(call[2] is target for call in runner.calls)
    assert report.attempted


@pytest.mark.parametrize(
    "steps,message",
    [
        ([("expand", {})], "missing required"),
        ([("expand", {"desired_replicas": 2, "force": True})], "does not accept"),
        ([("expand", {"desired_replicas": "two"})], "must be an integer"),
    ],
)
def test_invalid_parameters_reject_the_step_before_any_script_runs(target, steps, message):
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(make_decision(*steps))

    assert report.status == "not_executed"
    assert message in report.outcomes[0].error
    assert not runner.calls


def test_unknown_action_leaves_the_step_unmapped(target):
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(
        make_decision(("frobnicate", {}))
    )

    assert report.status == "not_executed"
    assert "no operation tool has action" in report.outcomes[0].error
    assert not runner.calls


def test_paths_outside_the_allowed_prefixes_stop_at_that_step(target):
    runner = FakeRunner(SkillRun(status="ok", payload={"status": "ok"}))
    plan = make_decision(
        ("expand", {"desired_replicas": 2}),
        ("preload", {"paths": ["/etc/passwd"]}),
    )

    report = PlanExecutor(
        target=target, runner=runner, allowed_path_prefixes=("/models/data",)
    ).execute(plan)

    assert report.status == "partial"
    assert [outcome.status for outcome in report.outcomes] == ["ok", "skipped"]
    assert "outside the allowed prefixes" in report.outcomes[1].error
    assert [call[0] for call in runner.calls] == ["expand_cache"]


def test_unmapped_first_step_stops_the_remainder(target):
    plan = make_decision(("frobnicate", {}), ("expand", {"desired_replicas": 2}))
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(plan)

    assert report.status == "not_executed"
    assert [outcome.status for outcome in report.outcomes] == ["skipped", "skipped"]
    assert "no operation tool has action" in report.outcomes[0].error
    assert "did not succeed" in report.outcomes[1].error
    assert not runner.calls


def test_partial_when_a_later_step_has_no_tool(target):
    plan = make_decision(("expand", {"desired_replicas": 2}), ("frobnicate", {}))
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(plan)

    assert report.status == "partial"
    assert [outcome.status for outcome in report.outcomes] == ["ok", "skipped"]


def test_failure_stops_later_steps_without_retry(approved, target):
    runner = FakeRunner(
        SkillRun(
            status="error",
            exit_code=1,
            payload={"status": "error", "reason_code": "kubernetes_409", "error": "conflict"},
            error="conflict",
        )
    )

    report = PlanExecutor(target=target, runner=runner).execute(approved)

    assert report.status == "failed"
    assert report.outcomes[0].status == "error"
    assert report.outcomes[0].error == "conflict"
    assert report.outcomes[0].exit_code == 1
    assert report.outcomes[1].status == "skipped"
    assert "step 0 did not succeed" in report.outcomes[1].error
    assert len(runner.calls) == 1


def test_unknown_outcome_is_reported_and_not_retried(approved, target):
    runner = FakeRunner(SkillRun(status="unknown", error="outcome undetermined"))

    report = PlanExecutor(target=target, runner=runner).execute(approved)

    assert report.status == "unknown"
    assert report.outcomes[1].status == "skipped"
    assert len(runner.calls) == 1


def test_non_action_decision_runs_nothing(target):
    noop = Decision.model_validate(
        {"status": "noop", "reason": "No action supported", "steps": []}
    )
    runner = FakeRunner()

    report = PlanExecutor(target=target, runner=runner).execute(noop)

    assert report.status == "not_required"
    assert not runner.calls


def test_dry_run_results_are_marked_and_not_counted_as_attempted(approved, target):
    runner = FakeRunner(
        SkillRun(status="ok", payload={"status": "ok", "dry_run": True}),
        SkillRun(status="ok", payload={"status": "ok", "dry_run": True}),
    )

    report = PlanExecutor(target=target, runner=runner).execute(approved)

    assert report.status == "completed"
    assert all(outcome.dry_run for outcome in report.outcomes)
    assert not report.attempted


def test_report_is_json_serializable_for_the_entry_point(approved, target):
    report = PlanExecutor(target=target, runner=FakeRunner()).execute(approved)

    assert isinstance(report, ExecutionReport)
    assert "expand_cache" in report.model_dump_json()
