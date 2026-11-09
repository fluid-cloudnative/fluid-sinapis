"""Verify the operation catalogue validates everything before a script can run."""

import pytest

from server.agent.execution import CATALOG, ExecutionPlanError, build_arguments, render_catalog
from server.agent.execution.catalog import normalize_arguments


def test_render_catalog_lists_every_action_with_its_parameters():
    text = render_catalog()

    for spec in CATALOG.values():
        assert spec.name in text
        assert f"action: {spec.action}" in text
        assert "never invent values" in text


def test_expand_arguments_include_host_bound_target(target):
    spec = CATALOG["expand_cache"]

    argv = build_arguments(spec, {"desired_replicas": 4}, target)

    assert argv == [
        "--kube-context", "test",
        "--namespace", "default",
        "--runtime-name", "training",
        "--runtime-plural", "alluxioruntimes",
        "--replicas", "4",
    ]


def test_shrink_reuses_the_scale_script_with_the_dangerous_flag(target):
    argv = build_arguments(CATALOG["shrink_cache"], {"desired_replicas": 1}, target)

    assert "--allow-shrink" in CATALOG["shrink_cache"].flags
    assert argv[-2:] == ["--replicas", "1"]


def test_preload_repeats_the_flag_for_each_path(target):
    argv = build_arguments(
        CATALOG["preload_cache"],
        {"paths": ["/nju-model/a.bin", "/nju-model/b.bin"]},
        target,
    )

    assert argv.count("--path") == 2
    assert "--dataset" in argv and "training" in argv


def test_defaults_are_applied_for_optional_parameters():
    normalized = normalize_arguments(
        CATALOG["preload_cache"], {"paths": ["/nju-model/a.bin"]}
    )

    assert normalized == {"paths": ["/nju-model/a.bin"], "replicas_per_path": 1}


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"desired_replicas": 2, "force": True}, "does not accept"),
        ({}, "missing required"),
        ({"desired_replicas": True}, "must be an integer"),
        ({"desired_replicas": 0}, ">= 1"),
    ],
)
def test_invalid_arguments_are_rejected(arguments, message):
    with pytest.raises(ExecutionPlanError, match=message):
        normalize_arguments(CATALOG["expand_cache"], arguments)


@pytest.mark.parametrize(
    "paths,message",
    [
        (["relative/path.bin"], "must be an absolute path"),
        (["/nju-model/../etc/passwd"], "must not contain"),
        (["/nju-model/a.bin", "/nju-model/a.bin"], "duplicate paths"),
        ([f"/nju-model/{index}.bin" for index in range(4)], "at most 3 items"),
        ([], "non-empty path list"),
    ],
)
def test_preload_paths_are_bounded(paths, message):
    with pytest.raises(ExecutionPlanError, match=message):
        normalize_arguments(CATALOG["preload_cache"], {"paths": paths})


def test_allowed_prefixes_block_other_dataset_paths():
    with pytest.raises(ExecutionPlanError, match="outside the allowed prefixes"):
        normalize_arguments(
            CATALOG["preload_cache"],
            {"paths": ["/other-model/a.bin"]},
            allowed_path_prefixes=("/nju-model",),
        )

    accepted = normalize_arguments(
        CATALOG["preload_cache"],
        {"paths": ["/nju-model/a.bin"]},
        allowed_path_prefixes=("/nju-model",),
    )
    assert accepted["paths"] == ["/nju-model/a.bin"]


def test_evict_requires_exactly_one_size_selector():
    spec = CATALOG["evict_cache"]

    assert normalize_arguments(spec, {"path": "/models", "count": 3})["count"] == 3
    with pytest.raises(ExecutionPlanError, match="exactly one of"):
        normalize_arguments(spec, {"path": "/models"})
    with pytest.raises(ExecutionPlanError, match="exactly one of"):
        normalize_arguments(spec, {"path": "/models", "count": 3, "target_bytes": 10})


def test_operation_id_must_be_safe_for_a_resource_name():
    with pytest.raises(ExecutionPlanError, match="does not match"):
        normalize_arguments(
            CATALOG["preload_cache"],
            {"paths": ["/nju-model/a.bin"], "operation_id": "bad id; rm -rf"},
        )


def test_evict_path_is_required_and_still_checked_against_the_allowlist():
    """evict has no default scan root: the model must name a directory, and "/" is
    rejected like any other out-of-allowlist path."""
    spec = CATALOG["evict_cache"]

    with pytest.raises(ExecutionPlanError, match="missing required path"):
        normalize_arguments(spec, {"target_bytes": 1024**3})

    with pytest.raises(ExecutionPlanError, match="outside the allowed prefixes"):
        normalize_arguments(
            spec,
            {"path": "/", "target_bytes": 1024**3},
            allowed_path_prefixes=("/models",),
        )

    # An explicit in-allowlist path works.
    accepted = normalize_arguments(
        spec,
        {"path": "/models/data", "target_bytes": 1024**3},
        allowed_path_prefixes=("/models",),
    )
    assert accepted["path"] == "/models/data"
