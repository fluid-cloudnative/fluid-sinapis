"""Structured execution mapping, per-step outcomes, and the round's execution report.

The two are clearly separated: `ExecutionPlan` is the **proposal** produced by the
deterministic mapping in code (each approved step bound to a concrete operation tool),
while `ExecutionReport` records **what the executor actually did**. The report is
built only from real subprocess results and never trusts the model's own account.
"""

from typing import Any, Literal

from pydantic import Field

from server.agent.schemas import Contract

# Per-step outcome: ok = the script completed normally; partial = partially done (for
# example only some files were evicted); error = definite failure; timeout = timed out
# (result unknown); unknown = the script output could not be classified (result
# unknown); skipped = not executed (no mapping, or stopped after an earlier failure)
StepStatus = Literal["ok", "partial", "error", "timeout", "unknown", "skipped"]

# Round outcome: not_required = nothing to execute (noop / insufficient_data / review
# rejected); not_executed = the plan had steps but none ran; completed = all succeeded;
# partial = some succeeded and some did not run; failed = a definite failure occurred;
# unknown = result unknown
ExecutionStatus = Literal[
    "not_required", "not_executed", "completed", "partial", "failed", "unknown"
]


class ExecutionCall(Contract):
    """Map one approved plan step to exactly one operation tool."""

    step: int = Field(ge=0)
    tool: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(Contract):
    """The execution agent's proposal; nothing here has been submitted yet."""

    reason: str = Field(min_length=1, max_length=2000)
    calls: list[ExecutionCall] = Field(default_factory=list)


class StepOutcome(Contract):
    """Record what one skill script actually did, or why it was not run."""

    step: int = Field(ge=0)
    action: str = Field(min_length=1)
    tool: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus
    exit_code: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    dry_run: bool = False


class ExecutionReport(Contract):
    """Summarize one execution attempt for the caller and the next round."""

    status: ExecutionStatus
    reason: str = Field(min_length=1, max_length=2000)
    outcomes: list[StepOutcome] = Field(default_factory=list)

    @property
    def attempted(self) -> bool:
        """Report whether any step really invoked a script (dry runs excluded)."""
        return any(
            outcome.status != "skipped" and not outcome.dry_run for outcome in self.outcomes
        )
