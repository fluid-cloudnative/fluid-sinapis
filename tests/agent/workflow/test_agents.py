"""Verify planning and review of extensible action plans."""

import json

import pytest
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from server.agent.workflow import AgentWorkflow


def test_approved_multi_step_decision_is_returned_verbatim(model_factory, decision):
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": True, "reason": "Consistent"}),
    )
    result = AgentWorkflow(model).run("Prepare the dataset")

    assert result.decision.model_dump() == decision
    assert result.execution is None


def test_reviewer_receives_context_observations_and_the_decision(model_factory, decision):
    context = {"ready_workers": 1, "available_files": ["/models/a.bin", "/models/b.bin"]}
    original_context = json.loads(json.dumps(context))
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": True, "reason": "Consistent"}),
    )

    result = AgentWorkflow(model).run("预加载模型并扩容", context)

    planner_input = json.loads(model.requests[0][-1].content)
    reviewer_input = json.loads(model.requests[1][-1].content)
    assert planner_input["request"] == "预加载模型并扩容"
    assert planner_input["context"] == context
    assert planner_input["observations"] == {}
    assert reviewer_input["request"] == "预加载模型并扩容"
    assert reviewer_input["context"] == context
    assert reviewer_input["observations_used"] == {}
    assert reviewer_input["fresh_observations"] == {}
    assert reviewer_input["decision"] == decision
    assert result.decision.model_dump() == decision
    assert context == original_context


def test_observations_are_collected_in_code_and_sent_to_both_roles(model_factory, decision):
    @tool
    def read_observation() -> dict:
        """Return a known observation for this workflow round."""
        return {"ready_workers": 2, "missing_metrics": ["file_access"]}

    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": False, "reason": "Missing file access"}),
    )

    result = AgentWorkflow(model, tools=[read_observation]).run("Prepare the cache")

    planner_input = json.loads(model.requests[0][-1].content)
    reviewer_input = json.loads(model.requests[1][-1].content)
    expected = {"read_observation": {
        "ready_workers": 2, "missing_metrics": ["file_access"],
    }}
    # The planner and the validator each get their own independently collected
    # observations (two code-side queries), compressed to the minimal form
    assert planner_input["observations"] == expected
    assert reviewer_input["observations_used"] == expected
    assert reviewer_input["fresh_observations"] == expected
    assert not result.validation.approved


def test_user_strategy_documents_and_script_verdicts_reach_both_roles(
    model_factory, decision, tmp_path
):
    user_dir = tmp_path / "user_skills"
    skill = user_dir / "hot-file-preload"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "自定义策略：带宽超阈值时建议 preload 热文件", encoding="utf-8"
    )
    (skill / "scripts" / "decide.py").write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'action': 'preload',\n"
        "                  'parameters': {'paths': ['/models/a.bin']},\n"
        "                  'reason': '自定义策略命中'}))\n",
        encoding="utf-8",
    )
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": True, "reason": "Follows user strategy"}),
    )

    AgentWorkflow(model, user_skills_dir=user_dir).run("Prepare the cache")

    planner_input = json.loads(model.requests[0][-1].content)
    reviewer_input = json.loads(model.requests[1][-1].content)
    assert "自定义策略：带宽超阈值" in planner_input["user_strategy_documents"]
    verdict = planner_input["custom_strategy_verdicts"][0]
    assert verdict["skill"] == "hot-file-preload"
    assert verdict["action"] == "preload"
    # The validator must see the user strategies too, to check "user wins on conflict"
    assert reviewer_input["user_strategy_documents"] == planner_input["user_strategy_documents"]
    assert reviewer_input["custom_strategy_verdicts"] == planner_input["custom_strategy_verdicts"]


def test_user_strategy_template_and_broken_scripts_do_not_block(model_factory, decision, tmp_path):
    user_dir = tmp_path / "user_skills"
    template = user_dir / "_template"
    (template / "scripts").mkdir(parents=True)
    (template / "SKILL.md").write_text("模板内容不应进入上下文", encoding="utf-8")
    (template / "scripts" / "decide.py").write_text("print('bad')", encoding="utf-8")
    broken = user_dir / "broken-strategy"
    (broken / "scripts").mkdir(parents=True)
    (broken / "SKILL.md").write_text("会崩溃的策略", encoding="utf-8")
    (broken / "scripts" / "decide.py").write_text("raise SystemExit(3)", encoding="utf-8")
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": True, "reason": "ok"}),
    )

    AgentWorkflow(model, user_skills_dir=user_dir).run("Prepare the cache")

    planner_input = json.loads(model.requests[0][-1].content)
    # Underscore-prefixed directories are skipped; a broken script reports an error without
    # blocking, and its details reach the context
    assert "模板内容" not in planner_input.get("user_strategy_documents", "")
    verdicts = planner_input["custom_strategy_verdicts"]
    assert verdicts[0]["skill"] == "broken-strategy"
    assert "error" in verdicts[0]


def test_user_script_printing_non_object_json_is_recorded_not_fatal(
    model_factory, decision, tmp_path
):
    user_dir = tmp_path / "user_skills"
    skill = user_dir / "loose-script"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("输出非对象 JSON 的策略", encoding="utf-8")
    (skill / "scripts" / "decide.py").write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps(['not', 'an', 'object']))\n",
        encoding="utf-8",
    )
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": True, "reason": "ok"}),
    )

    result = AgentWorkflow(model, user_skills_dir=user_dir).run("Prepare the cache")

    planner_input = json.loads(model.requests[0][-1].content)
    # Valid JSON that is not an object is recorded as a verdict error; the round survives
    verdict = planner_input["custom_strategy_verdicts"][0]
    assert verdict["skill"] == "loose-script"
    assert "not a JSON object" in verdict["error"]
    assert result.decision.model_dump() == decision


def test_validation_disabled_needs_only_one_request_and_runs_the_plan(
    model_factory, decision, target
):
    from unittest.mock import Mock

    from server.agent.execution import ExecutionReport

    planner = model_factory(("Decision", decision))
    executor = Mock()
    executor.execute.return_value = ExecutionReport(status="completed", reason="ok")

    result = AgentWorkflow(
        planner, executor=executor, validation_enabled=False
    ).run("Prepare the dataset", {"policies": {}})

    # Only one reasoning request; the validator model is never called
    assert len(planner.requests) == 1
    assert result.validation is None
    assert result.execution.status == "completed"
    executor.execute.assert_called_once()


def test_planner_and_validator_can_use_different_models(model_factory, decision):
    planner_model = model_factory(("Decision", decision))
    validator_model = model_factory(("PlanValidation", {"approved": True, "reason": "Consistent"}))

    result = AgentWorkflow(planner_model, validator_model).run("Prepare the dataset")

    assert result.validation.approved
    assert len(planner_model.requests) == 1
    assert len(validator_model.requests) == 1


def test_rejected_decision_is_returned_without_replanning(model_factory, decision, target):
    model = model_factory(
        ("Decision", decision),
        ("PlanValidation", {"approved": False, "reason": "The last expansion lacks evidence"}),
    )

    result = AgentWorkflow(model).run("Prepare the dataset")

    assert result.decision.model_dump() == decision
    assert not result.validation.approved
    assert result.validation.reason == "The last expansion lacks evidence"
    assert len(model.requests) == 2


@pytest.mark.parametrize("status", ["noop", "insufficient_data"])
def test_non_action_decisions_are_reviewed_but_not_executable(model_factory, status):
    model = model_factory(
        ("Decision", {"status": status, "reason": "No evidence for changes", "steps": []}),
        ("PlanValidation", {"approved": True, "reason": "Honest non-action"}),
    )

    result = AgentWorkflow(model).run("Check the dataset")

    assert result.decision.status == status
    assert result.execution is None
    assert len(model.requests) == 2


def test_invalid_later_step_stops_before_review(model_factory, decision):
    decision["steps"].append({"action": "", "parameters": {}})
    model = model_factory(("Decision", decision))

    with pytest.raises(StructuredOutputValidationError):
        AgentWorkflow(model).run("Prepare the dataset")

    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "review",
    [
        {"approved": "yes", "reason": "Invalid approval type"},
        {"approved": True, "reason": ""},
        {"approved": True, "reason": "Unexpected field", "execute": True},
    ],
)
def test_invalid_review_is_not_repaired_or_accepted(model_factory, decision, review):
    model = model_factory(("Decision", decision), ("PlanValidation", review))

    with pytest.raises(StructuredOutputValidationError):
        AgentWorkflow(model).run("Prepare the dataset")

    assert len(model.requests) == 2


@pytest.mark.parametrize("stage", ["planner", "validator"])
def test_plain_text_result_reports_the_stage_and_is_not_accepted(model_factory, decision, stage):
    # Some compatible APIs return plain text despite the required tool choice.
    plain_text = AIMessage(content="The plan looks reasonable.")
    responses = [plain_text] if stage == "planner" else [("Decision", decision), plain_text]
    model = model_factory(*responses)

    with pytest.raises(ValueError, match=f"{stage} did not return a structured"):
        AgentWorkflow(model).run("Prepare the dataset")

    assert len(model.requests) == len(responses)


@pytest.mark.parametrize("failing_stage", ["planner", "validator"])
def test_model_failure_propagates_without_retry(model_factory, decision, failing_stage):
    error = RuntimeError("model service unavailable")
    responses = [error] if failing_stage == "planner" else [("Decision", decision), error]
    model = model_factory(*responses)

    with pytest.raises(RuntimeError, match="model service unavailable") as raised:
        AgentWorkflow(model).run("Prepare the dataset")

    assert raised.value is error
    assert len(model.requests) == len(responses)


@pytest.mark.parametrize("user_request", ["", " \n\t"])
def test_blank_request_does_not_call_the_model(model_factory, user_request):
    model = model_factory()

    with pytest.raises(ValueError, match="request"):
        AgentWorkflow(model).run(user_request)

    assert not model.requests
