"""Plan from pre-assembled observations and independently review a fresh snapshot.

The planner can read historical cases before submitting its structured decision.
The validator receives the original observations and a second snapshot to cross-check.
The executor is deterministic code: each approved step maps to its catalogue tool by a
1:1 action lookup, with no model call in between.
"""

import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Any

from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import Field

from server.agent.cases import CaseStore
from server.agent.execution import PlanExecutor
from server.agent.execution.catalog import render_catalog
from server.agent.execution.schemas import ExecutionReport
from server.agent.prompts import (
    CASE_REFERENCE_PROMPT,
    PLANNER_PROMPT,
    VALIDATOR_PROMPT,
    WORKFLOW_REQUEST,
)
from server.agent.schemas import Contract, Decision

logger = logging.getLogger(__name__)

# Per-script timeout for user verdict scripts: they only do metric arithmetic, so a
# timeout means this round has no opinion from that strategy.
USER_SCRIPT_TIMEOUT_SECONDS = 60.0


def _read_skill_documents(
    directory: Path | None, *, fingerprints: dict[str, str] | None = None,
) -> str:
    """Concatenate every SKILL.md under the directory, keyed by skill name.

    Directories starting with an underscore (such as `_template`) are templates or
    drafts and never enter the model context.
    """
    if directory is None:
        return ""
    sections = []
    for skill_md in sorted(directory.glob("*/SKILL.md")):
        if skill_md.parent.name.startswith("_"):
            continue
        payload = skill_md.read_bytes()
        body = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if fingerprints is not None:
            fingerprints[skill_md.parent.name] = sha256(payload).hexdigest()
        sections.append(f"### {skill_md.parent.name}\n\n{body}")
    return "\n\n".join(sections)


def _collect_observations(tools: Sequence[Any]) -> list[dict[str, Any]]:
    """Query every observation source once; failures are recorded, never fabricated."""
    observations = []
    for tool in tools:
        try:
            observations.append({"tool": tool.name, "result": tool.invoke({})})
        except Exception as exc:  # noqa: BLE001 - record observation failures, never fabricate
            observations.append(
                {"tool": tool.name, "error": f"{type(exc).__name__}: {exc}"}
            )
    return observations


_TOOL_CALL_REMINDER = (
    "Your previous reply was plain text. Call the required tool again and return "
    "the complete structured arguments as its parameters; do not answer in prose."
)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _message_text(message: Any) -> str:
    """Flatten message content into plain text (a string or a list of parts)."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
    )


def _json_object_in_text(text: str) -> str | None:
    """Extract the outermost JSON object from a prose answer, if it contains one."""
    if not text:
        return None
    fenced = _JSON_FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    return candidate[start : end + 1]


def _case_replies(calls: list, tool: BaseTool, reads_left: int) -> list[ToolMessage]:
    replies = []
    for call in calls:
        if call.get("name") != tool.name:
            result = {"error": "Read the case replies, then submit the Decision tool by itself."}
        elif reads_left <= 0:
            result = {"error": "Case read limit reached."}
        else:
            reads_left -= 1
            try:
                arguments = call.get("args")
                if not isinstance(arguments, dict):
                    raise ValueError("expected tool arguments")
                result = tool.invoke(arguments)
            except ValueError:
                result = {"error": "Provide one or two distinct case IDs."}
        replies.append(ToolMessage(
            name=call.get("name"), tool_call_id=call["id"],
            content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        ))
    return replies


def _structured(
    model: BaseChatModel, schema, messages: list, stage: str, *, attempts: int = 2,
    case_tool: BaseTool | None = None,
):
    """Parse the structured reply after serving any bounded planner case reads.

    Case tool replies stay in the planner's thread. Case lookups have their own limit,
    leaving the existing structured output retry available after the last lookup.

    Even at temperature 0 the model occasionally answers in prose instead of
    emitting the tool call (llm.log: every failed planner round has non-empty
    content, every successful one is a bare tool call). Before giving up:
    - read a JSON object out of the prose and validate it against the schema;
    - retry once with an explicit tool-call reminder appended to the thread.
    None means every attempt failed; the prose is logged so the failure is
    attributable from the stage log alone.
    """
    base = list(messages)
    bound = model.bind_tools([schema, case_tool] if case_tool is not None else [schema])
    reads_left = 2 if case_tool is not None else 0
    attempt = 1
    while attempt <= attempts:
        message = bound.invoke(base)
        calls = getattr(message, "tool_calls", None) or []
        all_calls = calls + (getattr(message, "invalid_tool_calls", None) or [])
        case_calls = [] if case_tool is None else [
            call for call in all_calls if call.get("name") == case_tool.name
        ]
        if case_calls and reads_left:
            ids = [call.get("id") for call in all_calls]
            if all(ids) and len(set(ids)) == len(ids):
                base.append(message)
                base.extend(_case_replies(all_calls, case_tool, reads_left))
            else:
                base.append(HumanMessage(content="Use distinct tool call IDs for case reads."))
            reads_left = max(0, reads_left - len(case_calls))
            if not reads_left:
                bound = model.bind_tools([schema])
                base.append(HumanMessage(content="Return the final Decision alone now."))
            continue
        ambiguous_reply = case_tool is not None and len(all_calls) > 1
        call = None if case_calls or ambiguous_reply else next(
            (c for c in calls if c.get("name") == schema.__name__), None,
        )
        if call is not None:
            try:
                return schema.model_validate(call["args"])
            except Exception as exc:
                raise StructuredOutputValidationError(
                    f"{stage} returned an invalid {schema.__name__}: {exc}",
                    source=schema.__name__,
                    ai_message=message,
                ) from exc
        prose = _message_text(message).strip()
        raw = None if case_calls or ambiguous_reply else _json_object_in_text(prose)
        if raw:
            try:
                parsed = schema.model_validate_json(raw)
                logger.warning(
                    "[%s] attempt %d/%d: no %s tool call, recovered the structured "
                    "output from the message text",
                    stage, attempt, attempts, schema.__name__,
                )
                return parsed
            except Exception:
                pass
        logger.warning(
            "[%s] attempt %d/%d: no %s tool call, prose answer (%d chars): %.400s",
            stage, attempt, attempts, schema.__name__, len(prose), prose,
        )
        attempt += 1
        base.append(HumanMessage(content=_TOOL_CALL_REMINDER))
    return None


def _compact_observation(entry: dict[str, Any]) -> dict[str, Any]:
    """Compress one observation source into the smallest LLM-friendly form (user
    strategy scripts still receive the full structure)."""
    if "error" in entry:
        return {"error": entry["error"]}
    result = entry.get("result")
    if not isinstance(result, dict):
        return {"value": result}

    if "metrics" not in result:
        # Non-metric observation source: keep it as-is, only dropping empty values.
        return {key: value for key, value in result.items() if value not in (None, [], "")}

    files: dict[str, dict[str, Any]] = {}
    scalars: dict[str, Any] = {}
    observed_at_ms = 0
    for metric in result.get("metrics", []):
        name, value = metric.get("name"), metric.get("value")
        ts = metric.get("timestamp_ms") or 0
        observed_at_ms = max(observed_at_ms, ts or 0)
        file_path = (metric.get("labels") or {}).get("file")
        if file_path:
            # model_file_access_count -> access_count; other custom metrics keep their name.
            key = name.split("model_file_", 1)[-1] if name.startswith("model_file_") else name
            entry = files.setdefault(file_path, {})
            entry[key] = value
            if name == "model_file_last_access_time" and ts and value:
                # Recency is what the strategy documents reason about ("no access
                # within the statistics window"), and the model has no clock: derive
                # seconds-since-last-access from the sample time explicitly.
                entry["last_access_seconds_ago"] = round(max((ts - value) / 1000, 0), 1)
        else:
            scalars[name] = value

    compact: dict[str, Any] = {**scalars}
    if observed_at_ms:
        compact["observed_at_ms"] = observed_at_ms
    if files:
        compact["files"] = files
    missing = result.get("missing_metrics") or []
    if missing:
        compact["missing"] = missing  # Missing metrics appear as absent, never as zero
    return compact


def _compact_observations(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the compressed observations per source, keyed by source name."""
    return {entry["tool"]: _compact_observation(entry) for entry in observations}


# -- structured round results ---------------------------------- #


class PlanValidation(Contract):
    """Describe a model's review of a proposal, not a cluster execution outcome."""

    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class WorkflowResult(Contract):
    """Keep the proposed decision, its review, and the real execution report."""

    decision: Decision
    # None when independent review is disabled (agent.validation_enabled=false): the
    # decision goes straight to execution.
    validation: PlanValidation | None = None
    # Present only when the plan passed review and the action is "action"; the content
    # comes from real script results.
    execution: ExecutionReport | None = None


class AgentWorkflow:
    """Plan from a snapshot with optional case reads, then independently review."""

    def __init__(
        self,
        planner_model: BaseChatModel,
        validator_model: BaseChatModel | None = None,
        *,
        executor: PlanExecutor | None = None,
        tools: Sequence[Any] = (),
        skills_dir: str | Path | None = None,
        review_skills_dir: str | Path | None = None,
        user_skills_dir: str | Path | None = None,
        validation_enabled: bool = True,
        on_demand_cases: bool = False,
    ) -> None:
        # With false the validator is skipped: the plan goes straight to execution
        # (no second model call).
        self._validation_enabled = validation_enabled
        self._tools = list(tools)
        directory = Path(skills_dir).resolve() if skills_dir is not None else None
        if directory is not None and not directory.is_dir():
            raise ValueError(f"skill directory does not exist: {directory}")
        review_directory = (
            Path(review_skills_dir).resolve() if review_skills_dir is not None else directory
        )
        if review_directory is not None and not review_directory.is_dir():
            raise ValueError(f"review skill directory does not exist: {review_directory}")
        self._user_skills_dir = (
            Path(user_skills_dir).resolve() if user_skills_dir is not None else None
        )
        if self._user_skills_dir is not None and not self._user_skills_dir.is_dir():
            logger.warning(
                "User strategy directory unavailable at startup: %s", self._user_skills_dir,
            )

        self._on_demand_cases = on_demand_cases
        self._skills_dir = directory
        fingerprints: dict[str, str] = {}
        planning_documents = _read_skill_documents(
            directory, fingerprints=fingerprints if on_demand_cases else None,
        )
        self._case_rule_digests = {
            f"official/{name}": value for name, value in fingerprints.items()
        }
        review_documents = _read_skill_documents(review_directory)
        self._planner_system = (
            PLANNER_PROMPT + f"\n\n# Strategy skills\n\n{planning_documents}"
            if planning_documents
            else PLANNER_PROMPT
        )
        self._validator_system = (
            VALIDATOR_PROMPT + f"\n\n# Review skills\n\n{review_documents}"
            if review_documents
            else VALIDATOR_PROMPT
        )
        self._catalogue = render_catalog()
        validator_model = planner_model if validator_model is None else validator_model
        self._planner = planner_model
        self._validator = validator_model
        self._executor = executor

    def _load_user_strategies(
        self, *, fingerprints: dict[str, str] | None = None,
    ) -> tuple[str, list[Path]]:
        """Rescan the user strategy directory every round: documents go into the
        context and verdict scripts are run separately, which makes them hot-reloadable."""
        directory = self._user_skills_dir
        if directory is None or not directory.is_dir():
            return "", []
        documents = _read_skill_documents(directory, fingerprints=fingerprints)
        scripts = [
            path for path in sorted(directory.glob("*/scripts/*.py"))
            if not path.parent.parent.name.startswith("_")
        ]
        return documents, scripts

    def _run_user_strategy_scripts(
        self, scripts: Sequence[Path], observations: list[dict[str, Any]],
        context: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Run user verdict scripts: feed the observation snapshot on stdin, read the
        operation verdict from stdout.

        Failures (non-zero exit, timeout, bad JSON) are recorded without blocking; the
        model sees the failure details.
        """
        payload = json.dumps(
            {"observations": observations, "context": context},
            ensure_ascii=False, default=str,
        )
        verdicts: list[dict[str, Any]] = []
        for script in scripts:
            skill = script.parent.parent.name
            try:
                completed = subprocess.run(
                    [sys.executable, str(script)],
                    input=payload, capture_output=True, text=True,
                    timeout=USER_SCRIPT_TIMEOUT_SECONDS, check=False,
                )
            except Exception as exc:  # noqa: BLE001 - one failing script must not block the rest
                verdicts.append(
                    {"skill": skill, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if completed.returncode != 0:
                verdicts.append({
                    "skill": skill,
                    "error": f"exit {completed.returncode}: "
                             f"{completed.stderr.strip()[-300:]}",
                })
                continue
            try:
                verdict = json.loads(completed.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                verdicts.append({
                    "skill": skill,
                    "error": f"stdout is not valid JSON: {exc}; "
                             f"raw={completed.stdout.strip()[-200:]}",
                })
                continue
            if not isinstance(verdict, dict):
                # Valid JSON but not an object (a list, number, string...): record it
                # like any other malformed verdict instead of crashing the round.
                verdicts.append({
                    "skill": skill,
                    "error": "stdout is not a JSON object: "
                             f"raw={completed.stdout.strip()[-200:]}",
                })
                continue
            verdicts.append({"skill": skill, **verdict})
            logger.info(
                "[user strategy] %s verdict: action=%s", skill, verdict.get("action")
            )
        return verdicts

    def run(self, request: str, context: Mapping[str, Any] | None = None) -> WorkflowResult:
        """Plan and independently review a round before executing approved actions."""
        if not request.strip():
            raise ValueError("request must not be empty")
        context = dict(context or {})

        logger.info("Reading metrics from Prometheus")
        observations = _collect_observations(self._tools)
        # User-defined strategies reload every round: documents are injected into the
        # context and script verdicts act as the highest-priority evidence.
        user_fingerprints: dict[str, str] = {}
        user_documents, user_scripts = self._load_user_strategies(
            fingerprints=user_fingerprints if self._on_demand_cases else None,
        )
        verdicts = self._run_user_strategy_scripts(user_scripts, observations, context)
        if verdicts:
            summary = "; ".join(
                f"{v.get('skill')}→{v.get('action')}" for v in verdicts
            )
            logger.info("Custom strategy verdicts: %s", summary)

        planner_payload: dict[str, Any] = {
            "request": request,
            "context": context,
            # The model receives the compressed observations; user strategy scripts
            # receive the full structure above.
            "observations": _compact_observations(observations),
            "executable_actions": self._catalogue,
        }
        if user_documents:
            planner_payload["user_strategy_documents"] = user_documents
        if verdicts:
            planner_payload["custom_strategy_verdicts"] = verdicts
        planner_system = self._planner_system
        case_tool = None
        if self._on_demand_cases:
            rule_digests = self._case_rule_digests | {
                f"user/{name}": value for name, value in user_fingerprints.items()
            }
            cases = CaseStore.load(self._skills_dir, self._user_skills_dir, rule_digests)
            case_index = cases.index()
            if case_index:
                planner_payload["case_index"] = case_index
                planner_system += "\n" + CASE_REFERENCE_PROMPT
                case_tool = cases.tool()
        planner_messages = [
            SystemMessage(content=planner_system),
            HumanMessage(content=json.dumps(planner_payload, ensure_ascii=False, default=str)),
        ]
        logger.info("LLM planning")
        started = time.monotonic()
        decision = _structured(
            self._planner, Decision, planner_messages, "planner", case_tool=case_tool,
        )
        if decision is None:
            raise ValueError("planner did not return a structured Decision")
        logger.info(
            "LLM plan: status=%s, steps=%s, %.0fs",
            decision.status,
            [step.action for step in decision.steps],
            time.monotonic() - started,
        )
        logger.info("LLM rationale: %s", decision.reason)

        validation = None
        if self._validation_enabled:
            logger.info("LLM review in progress (re-querying metrics)")
            started = time.monotonic()
            fresh_observations = _collect_observations(self._tools)
            validator_payload: dict[str, Any] = {
                "request": request,
                "context": context,
                "observations_used": _compact_observations(observations),
                "fresh_observations": _compact_observations(fresh_observations),
                "decision": decision.model_dump(),
            }
            # The validator must see the user strategies to check whether the decision
            # followed "user wins on conflict".
            if user_documents:
                validator_payload["user_strategy_documents"] = user_documents
            if verdicts:
                validator_payload["custom_strategy_verdicts"] = verdicts
            validator_messages = [
                SystemMessage(content=self._validator_system),
                HumanMessage(
                    content=json.dumps(validator_payload, ensure_ascii=False, default=str)
                ),
            ]
            validation = _structured(
                self._validator, PlanValidation, validator_messages, "validator"
            )
            if validation is None:
                raise ValueError("validator did not return a structured PlanValidation")
            logger.info(
                "LLM review: approved=%s, %.0fs",
                validation.approved, time.monotonic() - started,
            )
            logger.info("Review rationale: %s", validation.reason)
        else:
            logger.info("LLM review is disabled, executing the plan as approved")

        approved = validation.approved if validation is not None else True
        execution = None
        if not approved:
            logger.info("Nothing to do (review did not approve)")
        elif decision.status != "action":
            logger.info("Nothing to do (status=%s)", decision.status)
        elif self._executor is None:
            logger.info("Nothing to do (no executor configured)")
        else:
            execution = self._executor.execute(decision)
            logger.info(
                "Fluid skills finished: status=%s, per step=%s",
                execution.status,
                [outcome.status for outcome in execution.outcomes],
            )
        return WorkflowResult(decision=decision, validation=validation, execution=execution)


def run_loop(
    workflow: AgentWorkflow,
    config,
    *,
    once: bool = False,
    stop: Event | None = None,
) -> None:
    """Run immediately, then wait between independent rounds until interrupted.

    A failed round is logged and the next interval starts with fresh model state.
    Single-round mode propagates failures so the caller gets a nonzero exit.

    Rounds never overlap: `workflow.run` is a blocking call, and its execution stage
    blocks on every skill script (which itself polls until the operation is really
    complete), so this loop cannot reach the next round while a previous round's
    operation is still in flight. The interval wait starts only after the round returned.
    """
    stop = stop if stop is not None else Event()
    context = {
        "target": config.target.model_dump(),
        "policies": config.policies,
    }
    round_no = 0
    while not stop.is_set():
        round_no += 1
        started = time.monotonic()
        logger.info("════ round %d start ════", round_no)
        try:
            result = workflow.run(WORKFLOW_REQUEST, context)
            print(result.model_dump_json(), flush=True)
            logger.info(
                "════ round %d end: approved=%s, execution=%s (%.0fs) ════",
                round_no,
                result.validation.approved if result.validation else "disabled",
                result.execution.status if result.execution else "not_run",
                time.monotonic() - started,
            )
        except Exception:
            if once:
                raise
            logger.exception("Agent round failed; waiting for the next interval")
        if once or stop.wait(config.agent.interval_seconds):
            break
