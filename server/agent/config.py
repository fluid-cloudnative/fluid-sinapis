"""Load configurations from a caller-selected YAML file."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from server.agent.schemas import Target
from server.config import reject_migrated_keys


class ConfigModel(BaseModel):
    """Reject misspelled settings without including credentials in validation errors."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelConfig(ConfigModel):
    """Connection settings for an OpenAI-compatible tool-calling chat model."""

    provider: Literal["openai", "openai_compatible"] = "openai_compatible"
    name: str = Field(min_length=1)
    api_key: SecretStr
    base_url: str | None = None
    temperature: float = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=60, gt=0)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(ConfigModel):
    """One planning/review pass per interval."""

    interval_seconds: float = Field(default=60, gt=0)
    # Enable the second, independent review model call; when false there is only one
    # reasoning pass and its result goes straight to execution.
    validation_enabled: bool = True


class PrometheusConfig(ConfigModel):
    """Read-only PromQL connection settings.

    Shares the ``prometheus`` section of config.yml with the collector:
    ``query_url`` is the read channel and ``pushgateway_url`` the collector's write
    channel, so the same address never has to be configured twice.
    """

    query_url: str = Field(default="http://localhost:9090", min_length=1)
    request_timeout_seconds: float = Field(default=10, gt=0)


class ExecutionConfig(ConfigModel):
    """Operation execution limits; the target and script paths stay host-controlled."""

    # Only file paths under these prefixes may reach a script; empty means no limit
    # (not advised).
    allowed_path_prefixes: list[str] = Field(default_factory=list)
    # Timeout for a single script; a timeout counts as an unknown result and is never retried.
    # Must stay above wait_timeout_seconds: a script polling for completion has to be
    # allowed to finish polling, otherwise a merely slow operation looks like a timeout.
    timeout_seconds: float = Field(default=660, gt=0)
    # Whether scripts wait for asynchronous work (replicas until Ready, DataLoad until
    # Complete). Enabled by default so a round only ends once the operation is really
    # done; turning it off makes completion reports mean "submitted" instead of "done".
    wait: bool = True
    wait_timeout_seconds: float = Field(default=600, gt=0)
    # Dry-run switch: print what would be submitted without touching the cluster.
    # Turn it on for the first integration round.
    dry_run: bool = False


class UserMetricConfig(ConfigModel):
    """A user-system metric the agent may observe (includes event-derived ones)."""

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    help: str = ""
    labels: tuple[str, ...] = Field(default_factory=tuple)
    kind: Literal["gauge", "counter"] = "gauge"


class UserSystemConfig(ConfigModel):
    """The agent's view of the config.yml ``user_system`` section.

    Receiving and storing values is the controller's job; this only answers "which
    metric names can be queried", expanding events into counter and gauge names
    according to their derivation rules.
    """

    metrics: tuple[UserMetricConfig, ...] = Field(default_factory=tuple)


class RuntimeConfig(ConfigModel):
    """Model connections and shared inputs for the skill-driven runtime."""

    planner_model: ModelConfig
    validator_model: ModelConfig
    agent: AgentConfig
    target: Target
    # Strategy skill directory used for planning and review.
    skills_dir: Path
    # Review-agent-only skill directory; falls back to skills_dir when unset.
    review_skills_dir: Path | None = None
    # User-defined strategy directory; a missing directory means no custom strategies.
    user_skills_dir: Path | None = None
    # Operation skill directory used for execution (scripts + SKILL.md).
    operation_skills_dir: Path
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    # Observable user-system metrics (from the user_system section, including event-derived ones).
    user_system: UserSystemConfig = Field(default_factory=UserSystemConfig)
    # Strategy preferences are interpreted by the planner through its skills.
    policies: dict[str, dict[str, Any]] = Field(default_factory=dict)


def load_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Resolve paths relative to the YAML file and merge per-agent model overrides.

    `model` supplies shared defaults; `models.planner` and `models.validator` override
    individual fields (there is no executor model: execution is deterministic code).
    Other application sections are left untouched.
    """
    path = Path(path).resolve()
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("config must contain a YAML mapping")
    reject_migrated_keys(data)
    e = os.environ if env is None else env

    defaults = _model_settings({}, data.get("model", {}), "model", e)
    models = _mapping(data.get("models", {}), "models")
    if models.keys() - {"planner", "validator"}:
        raise ValueError("models only accepts planner and validator")
    skills = _mapping(data.get("skills", {}), "skills")
    if skills.keys() - {"official_dir", "fluid_dir", "review_dir", "user_dir"}:
        raise ValueError(
            "skills only accepts official_dir, fluid_dir, review_dir and user_dir"
        )
    official_dir = _path_setting(
        skills.get("official_dir", "agent/skills/official"), "official_dir"
    )
    fluid_dir = _path_setting(
        skills.get("fluid_dir", "agent/skills/fluid_skills"), "fluid_dir"
    )
    # review_dir defaults to official_dir: when review skills are not split into their
    # own directory they are shared with planning.
    review_dir = _path_setting(
        skills.get("review_dir", official_dir), "review_dir"
    )
    user_dir = _path_setting(
        skills.get("user_dir", "agent/skills/user_skills"), "user_dir"
    )
    return RuntimeConfig(
        planner_model=ModelConfig.model_validate(
            _model_settings(defaults, models.get("planner", {}), "models.planner", e)
        ),
        validator_model=ModelConfig.model_validate(
            _model_settings(defaults, models.get("validator", {}), "models.validator", e)
        ),
        agent=AgentConfig.model_validate(data.get("agent", {})),
        target=Target.model_validate(data.get("target", {})),
        skills_dir=(path.parent / official_dir).resolve(),
        review_skills_dir=(path.parent / review_dir).resolve(),
        user_skills_dir=(path.parent / user_dir).resolve(),
        operation_skills_dir=(path.parent / fluid_dir).resolve(),
        prometheus=_prometheus_settings(data, e),
        execution=ExecutionConfig.model_validate(data.get("execution", {})),
        user_system=_user_system_settings(data),
        policies=_mapping(data.get("policies", {}), "policies"),
    )


# api_key: ${VAR} / $VAR - secrets live in environment variables only, never in
# version control.
_ENV_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

_PROMETHEUS_ENV: Mapping[str, str] = {
    "query_url": "PROMETHEUS_URL",
    "request_timeout_seconds": "PROMETHEUS_TIMEOUT_SECONDS",
}


def _model_settings(
    base: Mapping[str, Any],
    override: Any,
    where: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Merge shared defaults with the per-role override and expand the api_key
    environment reference before handing anything to the model."""
    settings = dict(base) | _mapping(override, where)
    api_key = settings.get("api_key")
    if isinstance(api_key, str):
        settings["api_key"] = _expand_secret(api_key, f"{where}.api_key", env)
    return settings


def _expand_secret(value: str, where: str, env: Mapping[str, str]) -> str:
    """Replace `${VAR}` with the environment value; fail loudly when it is unset
    instead of authenticating with a literal placeholder."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in env:
            raise ValueError(f"{where} references the unset environment variable {name}")
        return env[name]

    expanded = _ENV_REFERENCE.sub(replace, value)
    if "${" in expanded:
        raise ValueError(f"{where} has an unresolvable placeholder, write it as ${{VAR}}")
    return expanded


def _prometheus_settings(data: Mapping[str, Any], env: Mapping[str, str]) -> PrometheusConfig:
    """Read the prometheus section shared with the collector; env vars override per field."""
    section = _mapping(data.get("prometheus", {}), "prometheus")
    kwargs: dict[str, Any] = {}
    query_url = env.get(_PROMETHEUS_ENV["query_url"]) or section.get("query_url")
    if query_url is not None:
        kwargs["query_url"] = query_url
    raw_timeout = env.get(
        _PROMETHEUS_ENV["request_timeout_seconds"],
        section.get("request_timeout_seconds"),
    )
    if raw_timeout is not None:
        try:
            kwargs["request_timeout_seconds"] = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid prometheus.request_timeout_seconds: {raw_timeout!r}"
            ) from exc
    return PrometheusConfig(**kwargs)


def _path_setting(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"skills.{name} must be a non-empty path")
    return value


def _user_system_settings(data: Mapping[str, Any]) -> UserSystemConfig:
    """Derive the metric names the agent may observe from the user_system section,
    including event-derived counters and gauges.

    What the user's system pushes and what the controller stores is the receiving
    side's concern; this only fixes which names the agent can query. An invalid name
    or a clash with another derived name fails at startup rather than at runtime.
    """
    section = data.get("user_system")
    if section is None:
        return UserSystemConfig()
    if not isinstance(section, dict):
        raise ValueError("user_system must be a mapping")

    descriptors: list[UserMetricConfig] = []
    seen: set[str] = set()

    def add(raw_name: Any, help_text: Any, labels: Any, kind: str, where: str) -> None:
        try:
            descriptor = UserMetricConfig(
                name=raw_name,
                help=help_text if isinstance(help_text, str) else str(help_text),
                labels=tuple(str(label) for label in labels or ()),
                kind=kind,
            )
        except Exception as exc:
            raise ValueError(f"invalid {where}: {exc}") from exc
        if descriptor.name in seen:
            raise ValueError(f"duplicate derived metric name in user_system: {descriptor.name}")
        seen.add(descriptor.name)
        descriptors.append(descriptor)

    metrics = section.get("metrics") or []
    if not isinstance(metrics, list):
        raise ValueError("user_system.metrics must be a list")
    for index, item in enumerate(metrics):
        if not isinstance(item, dict):
            raise ValueError(f"user_system.metrics[{index}] must be a mapping")
        add(item.get("name"), item.get("help", ""), item.get("labels", ()),
            item.get("type", "gauge"), f"user_system.metrics[{index}]")

    events = section.get("events") or []
    if not isinstance(events, list):
        raise ValueError("user_system.events must be a list")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"user_system.events[{index}] must be a mapping")
        where = f"user_system.events[{index}]"
        labels = event.get("labels", ())
        add(event.get("counter"), f"{event.get('help', '')} (cumulative count)", labels,
            "counter", f"{where}.counter")
        gauges = event.get("gauges") or []
        if not isinstance(gauges, list):
            raise ValueError(f"{where}.gauges must be a list")
        for gindex, gauge in enumerate(gauges):
            if not isinstance(gauge, dict):
                raise ValueError(f"{where}.gauges[{gindex}] must be a mapping")
            add(gauge.get("metric"), gauge.get("help", ""), labels, "gauge",
                f"{where}.gauges[{gindex}]")

    return UserSystemConfig(metrics=tuple(descriptors))


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value
