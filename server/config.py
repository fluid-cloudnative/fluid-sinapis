"""System connection settings loaded from config.yml: target cluster / collection
semantics / Prometheus / scheduling.

The web console (web_controller.py) and the collector process (run_collectors.py)
share these connection settings; the single source is ``server/config.yml``.

The same file is also read by ``server/agent/config.py`` (agent side) and
``user_system_controller.py`` (external push), each picking its own sections.
The three do not cross-validate, so a value with one meaning may exist in
exactly one place:

- Cluster / namespace / dataset / Runtime live only in the ``target`` section,
  and are read from there by the collector, the web side and the agent;
- Prometheus endpoints live only in the ``prometheus`` section:
  ``pushgateway_url`` is the write channel, ``query_url`` the read channel.

Precedence: environment variables > config.yml > code defaults. Code defaults are
only a fallback for a missing yml key and carry no business convention; treat
config.yml as authoritative for anything cluster-related. Settings are read once at
process start and require a restart after editing (except ``user_system``, which is
reloaded live).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

import yaml

from server.agent.schemas import Target

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yml"

# Fallback used when config.yml has no target section (empty runtime_name = follow dataset).
DEFAULT_TARGET = Target(
    kube_context="kubernetes-admin@cluster",
    namespace="jgd-fluid",
    dataset="nju-model-hdfs",
    runtime_name="",
    runtime_plural="alluxioruntimes",
)


# ==============================================================
# Section settings: the field default doubles as the fallback and only
# applies when config.yml omits that key.
# ==============================================================
@dataclass(frozen=True)
class K8sSettings:
    """Collection semantics. Cluster targeting now lives in the target section."""

    node_role: str = "worker"
    # request = allocatable - scheduled requests; usage = real usage (needs metrics-server)
    free_mode: Literal["request", "usage"] = field(
        default="request", metadata={"choices": ("request", "usage")}
    )
    request_timeout_seconds: float = field(default=10.0, metadata={"gt": 0})


@dataclass(frozen=True)
class PrometheusSettings:
    """Metric channels: writes go to the pushgateway, reads go through PromQL."""

    pushgateway_url: str = "http://localhost:9091"
    query_url: str = "http://localhost:9090"
    request_timeout_seconds: float = field(default=10.0, metadata={"gt": 0})


@dataclass(frozen=True)
class CollectorSettings:
    """Collection schedule settings (run_collectors.py)."""

    interval_seconds: float = field(default=10.0, metadata={"gt": 0})


@dataclass(frozen=True)
class AppConfig:
    # Target is a pydantic model (and therefore unhashable), so a dataclass can only
    # accept it through default_factory.
    target: Target = field(default_factory=lambda: DEFAULT_TARGET)
    k8s: K8sSettings = field(default_factory=K8sSettings)
    prometheus: PrometheusSettings = field(default_factory=PrometheusSettings)
    collectors: CollectorSettings = field(default_factory=CollectorSettings)

    def resolved_target(self) -> Target:
        """Return a complete target, following dataset when runtime_name is empty."""
        if self.target.runtime_name:
            return self.target
        return self.target.model_copy(update={"runtime_name": self.target.dataset})

    @classmethod
    def load(
        cls,
        *,
        path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        """Load the effective settings; path/env are injectable for tests.

        Defaults to SINAPIS_CONFIG / os.environ.
        """
        e = os.environ if env is None else env
        override = e.get("SINAPIS_CONFIG", "").strip()
        explicit = bool(override) or path is not None
        config_path = Path(override) if override else Path(path or DEFAULT_CONFIG_PATH)
        raw = _read_yaml(config_path, warn_missing=explicit)
        reject_migrated_keys(raw)
        return cls(
            target=_build_target(raw, e),
            k8s=_build_section(K8sSettings, "k8s", raw, e),
            prometheus=_build_section(PrometheusSettings, "prometheus", raw, e),
            collectors=_build_section(CollectorSettings, "collectors", raw, e),
        )


# ==============================================================
# Merged-away legacy keys: seen means error, so that "changed but had no effect"
# cannot happen silently.
# ==============================================================
MIGRATED_KEYS: Mapping[str, Mapping[str, str]] = {
    "fluid": {
        "dataset": "target.dataset",
        "runtime_name": "target.runtime_name",
        "runtime_plural": "target.runtime_plural",
    },
    "k8s": {
        "kube_context": "target.kube_context",
        "namespace": "target.namespace",
    },
    "prometheus": {
        "prometheus_query_url": "prometheus.query_url",
    },
    "metrics": {
        "prometheus_url": "prometheus.query_url",
        "request_timeout_seconds": "prometheus.request_timeout_seconds",
    },
}


def reject_migrated_keys(raw: Mapping[str, Any]) -> None:
    """Fail loudly when a legacy key is still present instead of ignoring its value."""
    for section, moved in MIGRATED_KEYS.items():
        values = raw.get(section)
        if not isinstance(values, Mapping):
            continue
        for old_key, new_key in moved.items():
            if old_key in values:
                raise ValueError(
                    f"{section}.{old_key} has moved to {new_key}: "
                    f"delete {section}.{old_key} and put the value under {new_key}"
                )


# Field name -> environment variable name. Environment variables win over
# config.yml; the names follow the historical spelling.
_ENV_NAMES: Mapping[str, Mapping[str, str]] = {
    "target": {
        "kube_context": "KUBE_CONTEXT",
        "namespace": "KUBE_NS",
        "dataset": "FLUID_DATASET",
        "runtime_name": "FLUID_RUNTIME",
        "runtime_plural": "FLUID_RUNTIME_PLURAL",
    },
    "k8s": {
        "node_role": "K8S_NODE_ROLE",
        "free_mode": "K8S_FREE_MODE",
        "request_timeout_seconds": "K8S_TIMEOUT_SECONDS",
    },
    "prometheus": {
        "pushgateway_url": "PUSHGATEWAY_URL",
        "query_url": "PROMETHEUS_URL",
        "request_timeout_seconds": "PROMETHEUS_TIMEOUT_SECONDS",
    },
    "collectors": {
        "interval_seconds": "COLLECTOR_INTERVAL_SECONDS",
    },
}


def _build_target(raw: Mapping[str, Any], env: Mapping[str, str]) -> Target:
    """Build the target; it is the single source of cluster targeting for all sides."""
    values = raw.get("target")
    if values is not None and not isinstance(values, Mapping):
        raise ValueError("the target section must be a mapping")
    values = values if isinstance(values, Mapping) else {}

    merged = DEFAULT_TARGET.model_dump()
    for name, env_name in _ENV_NAMES["target"].items():
        if env_name in env:
            merged[name] = env[env_name]
        elif values.get(name) is not None:
            merged[name] = values[name]

    for name, value in merged.items():
        if not isinstance(value, str):
            raise ValueError(f"invalid target.{name} (a string is required): {value!r}")
    try:
        return Target(**merged)
    except ValueError as exc:  # pydantic's ValidationError is also a ValueError
        raise ValueError(f"invalid target section: {exc}") from exc


def _build_section(
    spec: type,
    section: str,
    raw: Mapping[str, Any],
    env: Mapping[str, str],
) -> Any:
    """Resolve each field as: environment variable > config.yml > field default."""
    defaults = spec()
    env_names = _ENV_NAMES.get(section, {})
    values = raw.get(section)
    if values is not None and not isinstance(values, Mapping):
        raise ValueError(f"the {section} section must be a mapping")
    values = values if isinstance(values, Mapping) else {}

    kwargs: dict[str, Any] = {}
    for f in fields(spec):
        default = getattr(defaults, f.name)
        env_name = env_names.get(f.name)
        if env_name and env_name in env:
            value: Any = env[env_name]
        elif values.get(f.name) is not None:
            value = values[f.name]
        else:
            value = default
        kwargs[f.name] = _coerce(section, f, default, value)
    return spec(**kwargs)


def _coerce(section: str, f: Any, default: Any, value: Any) -> Any:
    """Convert by the default's type, validate the range, and report "section.key"."""
    where = f"{section}.{f.name}"
    try:
        if isinstance(default, bool):
            coerced: Any = _to_bool(value)
        elif isinstance(default, float):
            coerced = float(value)
        elif isinstance(default, int):
            coerced = int(value)
        else:
            coerced = value if isinstance(value, str) else str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {where}: {value!r}") from exc

    choices = f.metadata.get("choices")
    if choices and coerced not in choices:
        raise ValueError(f"{where} must be one of {' / '.join(choices)}, got {coerced!r}")

    lower = f.metadata.get("gt")
    if lower is not None and not coerced > lower:
        raise ValueError(f"{where} must be greater than {lower}, got {coerced!r}")
    return coerced


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ValueError(f"not a valid boolean: {value!r}")


def _read_yaml(path: Path, *, warn_missing: bool) -> dict[str, Any]:
    """Missing file yields an empty mapping (code defaults apply); warn only if explicit."""
    if not path.is_file():
        if warn_missing:
            logger.warning("Config file not found, using defaults only: %s", path)
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"failed to parse the config file: {path} ({exc})") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, Mapping):
        raise ValueError(f"the config file must be a YAML mapping: {path}")
    return dict(parsed)
