"""Generic action plans and collector observations."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    """Reject unknown fields and implicit coercion in structured agent data."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Action(Contract):
    """Describe an intended operation without choosing its implementation."""

    action: str = Field(min_length=1, pattern=r"^\S+$")
    parameters: dict[str, Any] = Field(default_factory=dict)


class Decision(Contract):
    """Propose ordered actions without allowing the decision to select its target."""

    status: Literal["action", "noop", "insufficient_data"]
    reason: str = Field(min_length=1, max_length=2000)
    steps: list[Action]

    @model_validator(mode="after")
    def check_status(self) -> "Decision":
        if (self.status == "action") != bool(self.steps):
            raise ValueError("non-empty steps are required only for status=action")
        return self


class Target(Contract):
    """Identify the host-selected Fluid resources independently of the decision."""

    kube_context: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    # An empty runtime_name falls back to the dataset (see the validator below);
    # set it only when the Runtime and the Dataset have different names.
    runtime_name: str = ""
    # Kubernetes API resource name, e.g. alluxioruntimes or jindoruntimes.
    # Keep this explicit: CRD plural names cannot reliably be inferred from kind.
    runtime_plural: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9]*$")

    @model_validator(mode="after")
    def resolve_runtime_name(self) -> "Target":
        """Apply the empty-runtime_name-follows-dataset convention here, so every
        reader of a validated Target sees a fully resolved value and the fallback
        cannot be forgotten by one of them."""
        if not self.runtime_name:
            self.runtime_name = self.dataset
        return self


class Metric(Contract):
    """A labelled, typed observation value. Pure data with no prometheus dependency."""

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    help: str = Field(min_length=1)
    type: Literal["gauge", "counter"] = "gauge"
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    timestamp_ms: int | None = None


class CollectorSnapshot(Contract):
    """The complete output of one collection run by one collector."""

    collector: str
    identity: dict[str, str] = Field(default_factory=dict)
    metrics: list[Metric] = Field(default_factory=list)
