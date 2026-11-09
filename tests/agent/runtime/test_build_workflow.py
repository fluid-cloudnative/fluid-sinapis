"""Exercise configured models and Prometheus observations through the workflow."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent.config import RuntimeConfig
from server.agent.run_agents import build_workflow
from server.agent.prompts import WORKFLOW_REQUEST


@pytest.mark.parametrize("partial", [False, True])
def test_configured_runtime_reads_prometheus_into_both_snapshots(
    model_factory, target, prometheus_server, partial
):
    missing = []
    if partial:
        row = prometheus_server.payloads["fluid"]["data"]["result"].pop()
        missing = [row["metric"]["__name__"]]
    config = RuntimeConfig(
        planner_model={"name": "planner", "api_key": "planner-test"},
        validator_model={"name": "validator", "api_key": "validator-test"},
        agent={"validation_enabled": True},
        target=target,
        skills_dir=Path(__file__).resolve().parents[3] / "server/agent/skills/official",
        review_skills_dir=Path(__file__).resolve().parents[3]
        / "server/agent/skills/review_skills",
        operation_skills_dir=Path(__file__).resolve().parents[3]
        / "server/agent/skills/fluid_skills",
        prometheus={"query_url": prometheus_server.url},
    )
    planner = model_factory(
        ("Decision", {"status": "insufficient_data", "reason": "No file access data", "steps": []}),
    )
    reviewer = model_factory(("PlanValidation", {"approved": True, "reason": "Agreed"}))
    with patch(
        "server.agent.run_agents.create_model", side_effect=[planner, reviewer]
    ) as factory:
        result = build_workflow(config).run(WORKFLOW_REQUEST)

    assert result.execution is None
    assert result.validation.approved
    assert [call.args[0] for call in factory.call_args_list] == [
        config.planner_model,
        config.validator_model,
    ]
    # Each snapshot queries every configured observation source (the fixture has no
    # user_system, so fluid 6 + k8s 4): two snapshots in total
    assert len(prometheus_server.queries) == 20

    review_input = json.loads(reviewer.requests[0][-1].content)
    assert review_input["decision"]["status"] == "insufficient_data"

    used = review_input["observations_used"]["read_fluid_metrics"]
    fresh = review_input["fresh_observations"]["read_fluid_metrics"]
    # Compressed form: metric name -> value
    assert used["fluid_workers"] == 2
    assert fresh["fluid_workers"] == 2
    assert used["fluid_cache_used_bytes"] == fresh["fluid_cache_used_bytes"]
    # Missing metrics appear as absent keys and are never back-filled with zero
    if missing:
        assert missing[0] not in used
        assert missing[0] not in fresh
