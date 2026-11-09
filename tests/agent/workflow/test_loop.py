"""Verify round scheduling, stopping, and per-round failure handling."""

import json
from threading import Event
from unittest.mock import Mock

import pytest

from server.agent.config import RuntimeConfig
from server.agent.workflow import WorkflowResult, run_loop
from server.agent.prompts import WORKFLOW_REQUEST


@pytest.fixture
def config(target, tmp_path):
    return RuntimeConfig(
        planner_model={"name": "planner", "api_key": "test"},
        validator_model={"name": "validator", "api_key": "test"},
        agent={"interval_seconds": 5},
        target=target,
        skills_dir=tmp_path,
        operation_skills_dir=tmp_path,
    )


@pytest.fixture
def result():
    return WorkflowResult(
        decision={"status": "insufficient_data", "reason": "No file access data", "steps": []},
        validation={"approved": True, "reason": "Missing data is acknowledged"},
    )


def test_single_round_prints_structured_result_without_waiting(config, result, capsys):
    workflow = Mock()
    workflow.run.return_value = result
    stop = Mock(spec=Event)
    stop.is_set.return_value = False

    run_loop(workflow, config, once=True, stop=stop)

    workflow.run.assert_called_once_with(
        WORKFLOW_REQUEST,
        {
            "target": config.target.model_dump(),
            "policies": config.policies,
        },
    )
    stop.wait.assert_not_called()
    assert json.loads(capsys.readouterr().out) == result.model_dump()


def test_continuous_mode_waits_the_configured_interval_between_rounds(config, result, capsys):
    workflow = Mock()
    workflow.run.return_value = result
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]

    run_loop(workflow, config, stop=stop)

    assert workflow.run.call_count == 2
    assert [call.args for call in stop.wait.call_args_list] == [(5,), (5,)]
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_rounds_never_overlap_and_each_round_completes_before_the_next_starts(
    config, result
):
    """A later round must not begin while an earlier round is still executing skills."""
    events: list[str] = []
    workflow = Mock()

    def run(_request, _context):
        events.append("round-start")
        events.append("skills-running")
        events.append("round-end")
        return result

    workflow.run.side_effect = run
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]

    run_loop(workflow, config, stop=stop)

    assert events == ["round-start", "skills-running", "round-end"] * 2


def test_loop_passes_custom_policy_preferences_to_the_workflow(config, result):
    config.policies = {"preload": {"max_files": 2}, "expand": {"max_replicas": 5}}
    workflow = Mock()
    workflow.run.return_value = result

    run_loop(workflow, config, once=True)

    context = workflow.run.call_args.args[1]
    assert context["policies"]["preload"] == {"max_files": 2}
    assert context["policies"]["expand"] == {"max_replicas": 5}


def test_failed_round_does_not_end_continuous_mode(config, result, caplog):
    workflow = Mock()
    workflow.run.side_effect = [RuntimeError("model unavailable"), result]
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]

    run_loop(workflow, config, stop=stop)

    assert workflow.run.call_count == 2
    assert "Agent round failed" in caplog.text


def test_single_round_failure_propagates(config):
    workflow = Mock()
    workflow.run.side_effect = RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        run_loop(workflow, config, once=True)


def test_stopped_loop_does_not_call_the_workflow(config):
    workflow = Mock()
    stop = Event()
    stop.set()

    run_loop(workflow, config, stop=stop)

    workflow.run.assert_not_called()
