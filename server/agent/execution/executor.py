"""Turn an approved decision into real fluid_skills operations.

Mapping and execution are both deterministic code, keeping "model judgement" and
"cluster mutation" apart:

1. Mapping stage (code): each approved step is looked up in the catalogue by action
   (the mapping is 1:1) and its parameters are validated against the spec. A step with
   no exactly matching tool stays unmapped. Parameter values are never altered or
   filled in: the reviewed decision is the only source of values.
2. Execution stage (code): mapped steps run in plan order; the first unmapped or
   unsuccessful step stops the run, later steps are marked skipped, and there is never
   an automatic retry or a rollback of submitted steps.

Reports are built only from real subprocess results.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from server.agent.schemas import Action, Decision, Target

from .catalog import CATALOG, ExecutionPlanError, ToolSpec, normalize_arguments
from .schemas import (
    ExecutionCall,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
    StepOutcome,
)
from .skill_runner import SupportsSkillRun

logger = logging.getLogger(__name__)

_NO_MAPPING = "no catalogue tool matches this step, so it was left unmapped"


class PlanExecutor:
    """Map an approved plan through the catalogue, then run it with code checks."""

    def __init__(
        self,
        *,
        target: Target,
        runner: SupportsSkillRun,
        catalog: Mapping[str, ToolSpec] | None = None,
        allowed_path_prefixes: Sequence[str] = (),
    ) -> None:
        self._catalog: dict[str, ToolSpec] = dict(catalog or CATALOG)
        self._target = target
        self._runner = runner
        self._prefixes = tuple(allowed_path_prefixes)

    # -- public entry point --------------------------------------- #
    def execute(self, decision: Decision) -> ExecutionReport:
        """Execute an approved action decision, or explain why nothing was executed."""
        if decision.status != "action" or not decision.steps:
            return ExecutionReport(
                status="not_required",
                reason=f"no steps to execute (status={decision.status})",
            )

        assignments, unmapped_reasons = self._assign(decision)
        return self._execute_steps(
            decision, self._direct_plan(assignments), assignments, unmapped_reasons
        )

    # -- mapping stage -------------------------------------------- #
    def _assign(
        self, decision: Decision
    ) -> tuple[dict[int, tuple[ToolSpec, dict[str, Any]]], dict[int, str]]:
        """Map each step to its catalogue tool by action and validate its parameters.

        Returns the valid assignments plus, for every unmapped step, the reason it
        could not be mapped. A step whose action has no single tool, or whose
        parameters fail validation, stays unmapped; `_execute_steps` stops the run
        there and later steps are skipped.
        """
        assignments: dict[int, tuple[ToolSpec, dict[str, Any]]] = {}
        reasons: dict[int, str] = {}
        for index, step in enumerate(decision.steps):
            candidates = [
                spec for spec in self._catalog.values() if spec.action == step.action
            ]
            if not candidates:
                reasons[index] = f"no operation tool has action {step.action!r}"
                continue
            if len(candidates) > 1:
                reasons[index] = (
                    f"action {step.action!r} matches several tools; mapping is 1:1 only"
                )
                continue
            try:
                assignments[index] = (
                    candidates[0],
                    normalize_arguments(
                        candidates[0], step.parameters, allowed_path_prefixes=self._prefixes
                    ),
                )
            except ExecutionPlanError as exc:
                reasons[index] = f"step parameters are invalid: {exc}"
                logger.warning("Rejected mapping for step %d: %s", index, exc)
        return assignments, reasons

    def _direct_plan(
        self, assignments: dict[int, tuple[ToolSpec, dict[str, Any]]]
    ) -> ExecutionPlan:
        """The deterministic mapping expressed as a plan, for uniform reporting."""
        return ExecutionPlan(
            reason="deterministic mapping: every mapped step passed catalogue validation "
            "on action and parameters alone",
            calls=[
                ExecutionCall(step=index, tool=spec.name, parameters=parameters)
                for index, (spec, parameters) in sorted(assignments.items())
            ],
        )

    # -- execution stage ------------------------------------------ #
    def _execute_steps(
        self,
        decision: Decision,
        plan: ExecutionPlan,
        assignments: dict[int, tuple[ToolSpec, dict[str, Any]]],
        unmapped_reasons: dict[int, str],
    ) -> ExecutionReport:
        outcomes: list[StepOutcome] = []
        stopped_at: int | None = None
        for index, step in enumerate(decision.steps):
            if index not in assignments:
                # An unmapped step also stops the rest: plan order is dependency order,
                # and running a later step after skipping an earlier one could violate
                # a premise the review already approved.
                outcomes.append(
                    self._skipped(index, step, unmapped_reasons.get(index, _NO_MAPPING))
                )
                stopped_at = index if stopped_at is None else stopped_at
                continue
            if stopped_at is not None:
                outcomes.append(
                    self._skipped(
                        index,
                        step,
                        f"step {stopped_at} did not succeed, so this step was not run",
                    )
                )
                continue
            spec, parameters = assignments[index]
            logger.info("Running Fluid skill %s (step %d)", spec.name, index)
            run = self._runner.run(spec, parameters, target=self._target)
            outcomes.append(
                StepOutcome(
                    step=index,
                    action=step.action,
                    tool=spec.name,
                    parameters=parameters,
                    status=run.status,
                    exit_code=run.exit_code,
                    payload=dict(run.payload),
                    error=run.error,
                    dry_run=bool(run.payload.get("dry_run", False)),
                )
            )
            if run.status != "ok":
                stopped_at = index

        return ExecutionReport(
            status=_overall_status(outcomes),
            reason=_summary(plan, outcomes),
            outcomes=outcomes,
        )

    @staticmethod
    def _skipped(index: int, step: Action, reason: str) -> StepOutcome:
        return StepOutcome(
            step=index, action=step.action, status="skipped", error=reason[:500]
        )


def _overall_status(outcomes: Sequence[StepOutcome]) -> ExecutionStatus:
    ran = [outcome for outcome in outcomes if outcome.status != "skipped"]
    if not ran:
        return "not_executed"
    if any(outcome.status in {"unknown", "timeout"} for outcome in outcomes):
        return "unknown"
    if any(outcome.status in {"error", "partial"} for outcome in outcomes):
        return "failed"
    if all(outcome.status == "ok" for outcome in outcomes):
        return "completed"
    return "partial"


def _summary(plan: ExecutionPlan, outcomes: Sequence[StepOutcome]) -> str:
    detail = "; ".join(
        f"step {outcome.step} {outcome.action}->{outcome.tool or 'unmapped'}: {outcome.status}"
        for outcome in outcomes
    )
    return f"{plan.reason} | {detail}"[:2000]


__all__ = ["PlanExecutor"]
