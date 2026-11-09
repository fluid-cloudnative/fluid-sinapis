"""Declare the fluid_skills operations as a constrained catalogue.

The deterministic executor maps each approved plan action to exactly one tool from
this catalogue and validates its declared parameters: the catalogue is both the
manual shown to the planning model and the allow list enforced in code. Every
parameter is validated before a script is spawned, so a plan cannot rewrite the
target, append script arguments, or escape the path allow list.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from server.agent.schemas import Target

ParamKind = Literal["int", "str", "path", "path_list"]

_TARGET_FLAGS = {
    "kube_context": "--kube-context",
    "namespace": "--namespace",
    "dataset": "--dataset",
    "runtime_name": "--runtime-name",
    "runtime_plural": "--runtime-plural",
}


class ExecutionPlanError(ValueError):
    """Reject a mapping or its parameters before anything is submitted."""


@dataclass(frozen=True)
class Parameter:
    """One script argument that a model is allowed to supply."""

    name: str
    kind: ParamKind
    flag: str
    description: str
    required: bool = False
    default: Any = None
    minimum: int | None = None
    maximum_items: int | None = None
    pattern: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """Bind one plan action to one skill script and its allowed parameters."""

    name: str
    action: str
    summary: str
    skill: str
    script: str
    parameters: tuple[Parameter, ...]
    # target fields come from host configuration; the model may read but not change them
    target_fields: tuple[str, ...] = ()
    # fixed flags (shrink must be explicitly allowed), which the model cannot add or drop
    flags: tuple[str, ...] = ()
    wait_supported: bool = False
    require_one_of: tuple[str, ...] = ()

    def parameter(self, name: str) -> Parameter | None:
        for item in self.parameters:
            if item.name == name:
                return item
        return None


def _param(
    name: str, kind: ParamKind, flag: str, description: str, **kwargs: Any
) -> Parameter:
    return Parameter(name=name, kind=kind, flag=flag, description=description, **kwargs)


CATALOG: dict[str, ToolSpec] = {
    "expand_cache": ToolSpec(
        name="expand_cache",
        action="expand",
        summary="Scale the target Fluid Runtime up to an absolute worker replica count.",
        skill="fluid-cache-scale-up",
        script="fluid-cache-scale-up/scripts/scale_up.py",
        parameters=(
            _param(
                "desired_replicas",
                "int",
                "--replicas",
                "Absolute total replicas after scaling, not the increment.",
                required=True,
                minimum=1,
            ),
        ),
        target_fields=("kube_context", "namespace", "runtime_name", "runtime_plural"),
        wait_supported=True,
    ),
    "shrink_cache": ToolSpec(
        name="shrink_cache",
        action="shrink",
        summary="Scale the target Fluid Runtime down to an absolute worker replica count.",
        skill="fluid-cache-scale-up",
        script="fluid-cache-scale-up/scripts/scale_up.py",
        parameters=(
            _param(
                "desired_replicas",
                "int",
                "--replicas",
                "Absolute total replicas after scaling, keep at least one worker.",
                required=True,
                minimum=1,
            ),
        ),
        target_fields=("kube_context", "namespace", "runtime_name", "runtime_plural"),
        # The same script refuses to shrink by default; only a shrink action allows it.
        flags=("--allow-shrink",),
        wait_supported=True,
    ),
    "preload_cache": ToolSpec(
        name="preload_cache",
        action="preload",
        summary="Create a DataLoad to preload dataset paths into the Fluid cache.",
        skill="fluid-cache-preload",
        script="fluid-cache-preload/scripts/preload.py",
        parameters=(
            _param(
                "paths",
                "path_list",
                "--path",
                "Dataset paths to preload; one cache replica is requested per path.",
                required=True,
                maximum_items=3,
            ),
            _param(
                "replicas_per_path",
                "int",
                "--replicas-per-path",
                "Cache replicas requested for each path.",
                default=1,
                minimum=1,
            ),
            _param(
                "operation_id",
                "str",
                "--operation-id",
                "Optional stable identifier; the same id always maps to the same DataLoad.",
                pattern=r"[A-Za-z0-9._-]{1,64}",
            ),
        ),
        # A DataLoad points at the Dataset; Fluid resolves the bound Runtime itself,
        # so no runtime fields are passed to the script.
        target_fields=(
            "kube_context",
            "namespace",
            "dataset",
        ),
        wait_supported=True,
    ),
    "evict_cache": ToolSpec(
        name="evict_cache",
        action="evict",
        summary=(
            "Free cached files under a scan root using the skill's LRU order. "
            "This tool cannot free exact paths: it only releases roughly target_bytes "
            "or count files, so it may evict files other than the planned ones."
        ),
        skill="fluid-cache-evict-lru",
        script="fluid-cache-evict-lru/scripts/evict_lru.py",
        parameters=(
            _param(
                "path",
                "path",
                "--path",
                "Directory scanned for cached files; required, keep it inside the allowed "
                "prefixes and never pass \"/\".",
                required=True,
            ),
            _param("target_bytes", "int", "--target-bytes", "Bytes to free.", minimum=1),
            _param("count", "int", "--count", "Number of cached files to free.", minimum=1),
        ),
        target_fields=("kube_context", "namespace", "dataset"),
        require_one_of=("target_bytes", "count"),
    ),
}

def render_catalog(catalog: Mapping[str, ToolSpec] | None = None) -> str:
    """Render the catalogue for the planning model's prompt."""
    lines = [
        "Available operation tools. Each plan step maps to at most one call; the plan's "
        "action must match the tool's action, and parameters must come from that step."
    ]
    for spec in (CATALOG if catalog is None else catalog).values():
        lines.append(f"- {spec.name} (action: {spec.action}) — {spec.summary}")
        for parameter in spec.parameters:
            requirement = "required" if parameter.required else "optional"
            bounds = f", >= {parameter.minimum}" if parameter.minimum is not None else ""
            if parameter.maximum_items is not None:
                bounds += f", 1..{parameter.maximum_items} items"
            default = "" if parameter.default is None else f", default {parameter.default!r}"
            lines.append(
                f"    {parameter.name}: {parameter.kind} ({requirement}{bounds}{default}) "
                f"{parameter.description}"
            )
    lines.append(
        "A step whose action has no tool above stays unmapped. Never substitute another "
        "action and never invent values that the step does not provide."
    )
    return "\n".join(lines)


def normalize_arguments(
    spec: ToolSpec, raw: dict[str, Any], *, allowed_path_prefixes: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Validate every supplied argument against the spec; raise before anything runs."""
    known = {parameter.name for parameter in spec.parameters}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ExecutionPlanError(
            f"{spec.name} does not accept {unknown}; allowed parameters are {sorted(known)}"
        )

    normalized: dict[str, Any] = {}
    for parameter in spec.parameters:
        if parameter.name not in raw:
            if parameter.required:
                raise ExecutionPlanError(f"{spec.name} is missing required {parameter.name}")
            if parameter.default is not None:
                # Defaults go through the SAME validation as supplied values: no default
                # may bypass the path allowlist.
                normalized[parameter.name] = _normalize_value(
                    spec, parameter, parameter.default, allowed_path_prefixes
                )
            continue
        normalized[parameter.name] = _normalize_value(
            spec, parameter, raw[parameter.name], allowed_path_prefixes
        )

    if spec.require_one_of:
        chosen = [name for name in spec.require_one_of if normalized.get(name) is not None]
        if len(chosen) != 1:
            raise ExecutionPlanError(
                f"{spec.name} requires exactly one of {list(spec.require_one_of)}"
            )
    return normalized


def _normalize_value(
    spec: ToolSpec, parameter: Parameter, value: Any, allowed_path_prefixes: tuple[str, ...]
) -> Any:
    if parameter.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExecutionPlanError(f"{spec.name}.{parameter.name} must be an integer")
        if parameter.minimum is not None and value < parameter.minimum:
            raise ExecutionPlanError(
                f"{spec.name}.{parameter.name} must be >= {parameter.minimum}, got {value}"
            )
        return value

    if parameter.kind == "path":
        return _normalize_path(spec, parameter, value, allowed_path_prefixes)

    if parameter.kind == "path_list":
        if not isinstance(value, list) or not value:
            raise ExecutionPlanError(f"{spec.name}.{parameter.name} must be a non-empty path list")
        if parameter.maximum_items is not None and len(value) > parameter.maximum_items:
            raise ExecutionPlanError(
                f"{spec.name}.{parameter.name} takes at most {parameter.maximum_items} "
                f"items, got {len(value)}"
            )
        paths = [
            _normalize_path(spec, parameter, item, allowed_path_prefixes) for item in value
        ]
        if len(set(paths)) != len(paths):
            raise ExecutionPlanError(f"{spec.name}.{parameter.name} contains duplicate paths")
        return paths

    if not isinstance(value, str) or not value.strip():
        raise ExecutionPlanError(f"{spec.name}.{parameter.name} must be a non-empty string")
    text = value.strip()
    if parameter.pattern is not None and not re.fullmatch(parameter.pattern, text):
        raise ExecutionPlanError(
            f"{spec.name}.{parameter.name} does not match {parameter.pattern}: {text!r}"
        )
    return text


def _normalize_path(
    spec: ToolSpec, parameter: Parameter, value: Any, allowed_path_prefixes: tuple[str, ...]
) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ExecutionPlanError(
            f"{spec.name}.{parameter.name} must be an absolute path, got {value!r}"
        )
    path = value.rstrip("/") or "/"
    if ".." in path.split("/"):
        raise ExecutionPlanError(f"{spec.name}.{parameter.name} must not contain '..': {value!r}")
    if allowed_path_prefixes:
        for prefix in allowed_path_prefixes:
            root = prefix.rstrip("/")
            if path == root or path.startswith(root + "/"):
                return path
        raise ExecutionPlanError(
            f"{spec.name}.{parameter.name} path {path} is outside the allowed prefixes "
            f"{list(allowed_path_prefixes)}"
        )
    return path


def build_arguments(spec: ToolSpec, arguments: dict[str, Any], target: Target) -> list[str]:
    """Turn validated arguments plus host-bound target into script arguments."""
    argv: list[str] = []
    for field in spec.target_fields:
        value = getattr(target, field, None)
        if not value:
            raise ExecutionPlanError(f"target.{field} is empty, cannot run {spec.name}")
        argv += [_TARGET_FLAGS[field], str(value)]
    for parameter in spec.parameters:
        value = arguments.get(parameter.name)
        if value is None:
            continue
        if parameter.kind == "path_list":
            for item in value:
                argv += [parameter.flag, item]
        else:
            argv += [parameter.flag, str(value)]
    return argv
