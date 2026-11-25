"""Exercise case reads through the normal snapshot planning workflow."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from server.agent.workflow import AgentWorkflow

CASE_ID = "user.case-test"
NOOP = {"status": "noop", "reason": "No currently accessed files", "steps": []}


def test_case_and_early_decision_receive_separate_matching_tool_replies(
    model_factory, tmp_path, write_case,
):
    write_case(tmp_path, CASE_ID)
    calls = []
    replies = [
        [("read_cases", {"ids": [CASE_ID]}), ("Decision", {**NOOP, "reason": "Early"})],
        [("Decision", NOOP)],
    ]

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-case-test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": f"call-{i}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(parameters),
                    }}
                    for i, (name, parameters) in enumerate(replies.pop(0))
                ],
            }}],
        })

    snapshots = []

    def observe():
        """Provide real values for this round's snapshots."""
        snapshots.append(len(snapshots))
        return {"files_seen": snapshots[-1]}

    review = model_factory(("PlanValidation", {"approved": True, "reason": "Checked"}))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        planner = ChatOpenAI(
            model="test", api_key="test", base_url="https://model.example/v1",
            http_client=http_client,
        )
        workflow = AgentWorkflow(
            planner, review, tools=[StructuredTool.from_function(observe)],
            user_skills_dir=tmp_path, on_demand_cases=True,
        )
        result = workflow.run("plan")

    assert result.decision.model_dump() == NOOP
    assert result.validation.approved
    assert snapshots == [0, 1]
    first = calls[0]
    assert [tool["function"]["name"] for tool in first["tools"]] == ["Decision", "read_cases"]
    payload = json.loads(first["messages"][-1]["content"])
    assert payload["observations"] == {"observe": {"files_seen": 0}}
    assert [row["id"] for row in payload["case_index"]] == [CASE_ID]
    assert all(set(row) == {"id", "when"} for row in payload["case_index"])
    messages = calls[1]["messages"]
    assert [message["role"] for message in messages[-3:]] == ["assistant", "tool", "tool"]
    case_reply, early_reply = messages[-2:]
    assert (case_reply["tool_call_id"], early_reply["tool_call_id"]) == ("call-0", "call-1")
    assert json.loads(case_reply["content"])["cases"][0]["id"] == CASE_ID
    assert "Decision tool by itself" in early_reply["content"]
    review_payload = json.loads(review.requests[0][-1].content)
    assert CASE_ID not in review.requests[0][-1].content
    assert "case_index" not in review_payload
    assert review_payload["observations_used"] == {"observe": {"files_seen": 0}}
    assert review_payload["fresh_observations"] == {"observe": {"files_seen": 1}}


@pytest.mark.parametrize("enabled", [False, True])
def test_direct_decision_uses_one_planning_request(model_factory, tmp_path, write_case, enabled):
    write_case(tmp_path, CASE_ID)
    planner = model_factory(("Decision", NOOP))
    result = AgentWorkflow(
        planner, user_skills_dir=tmp_path, on_demand_cases=enabled, validation_enabled=False,
    ).run("plan")

    assert result.decision.model_dump() == NOOP
    assert len(planner.requests) == 1
    payload = json.loads(planner.requests[0][-1].content)
    assert ("case_index" in payload) is enabled
    assert ("Historical cases" in planner.requests[0][0].content) is enabled


def test_invalid_and_excess_case_calls_can_still_finish_with_decision(
    model_factory, tmp_path, write_case,
):
    write_case(tmp_path, CASE_ID)
    requests = AIMessage(content="", tool_calls=[
        {"name": "read_cases", "args": {"ids": ["../invalid"]}, "id": "invalid"},
        {"name": "read_cases", "args": {"ids": ["user.unknown"]}, "id": "missing"},
        {"name": "read_cases", "args": {"ids": [CASE_ID]}, "id": "excess"},
    ])
    planner = model_factory(requests, AIMessage(content="A conclusion"), ("Decision", NOOP))
    result = AgentWorkflow(
        planner, user_skills_dir=tmp_path, on_demand_cases=True, validation_enabled=False,
    ).run("plan")

    assert result.decision.model_dump() == NOOP
    replies = [message for message in planner.requests[1] if isinstance(message, ToolMessage)]
    assert [reply.tool_call_id for reply in replies] == ["invalid", "missing", "excess"]
    assert "distinct case IDs" in replies[0].content
    assert json.loads(replies[1].content)["unavailable"] == [
        {"id": "user.unknown", "code": "not_found"},
    ]
    assert "limit reached" in replies[2].content
    assert "Call the required tool" in planner.requests[2][-1].content


def test_user_case_without_rules_reloads_with_user_verdict(model_factory, tmp_path, write_case):
    planner = model_factory(("Decision", NOOP), ("Decision", NOOP))
    strategy = tmp_path / "custom"
    scripts = strategy / "scripts"
    scripts.mkdir(parents=True)
    (strategy / "SKILL.md").write_text("Custom rules override preload", encoding="utf-8")
    (scripts / "decide.py").write_text(
        'print(\'{"action":"noop","reason":"Custom rule"}\')\n', encoding="utf-8",
    )
    workflow = AgentWorkflow(
        planner, user_skills_dir=tmp_path, on_demand_cases=True, validation_enabled=False,
    )
    workflow.run("plan")
    write_case(tmp_path, "user.listing-delay")
    workflow.run("plan")

    first, second = [json.loads(request[-1].content) for request in planner.requests]
    assert "case_index" not in first
    assert second["case_index"][0]["id"] == "user.listing-delay"
    assert first["custom_strategy_verdicts"] == second["custom_strategy_verdicts"] == [
        {"skill": "custom", "action": "noop", "reason": "Custom rule"},
    ]
