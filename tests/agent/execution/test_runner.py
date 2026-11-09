"""Verify subprocess construction and outcome classification for skill scripts."""

import json
import subprocess
from dataclasses import replace

import pytest

from server.agent.execution import CATALOG, SkillRunner


@pytest.fixture
def skills_root(tmp_path):
    """Create the minimal script tree the catalogue expects."""
    for spec in CATALOG.values():
        script = tmp_path / spec.script
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# stub\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def recorded(monkeypatch):
    """Capture the command and return a chosen CompletedProcess."""
    state = {"command": None, "timeout": None,
             "result": subprocess.CompletedProcess([], 0, "{}", "")}

    def fake_run(command, **kwargs):
        state["command"] = command
        state["timeout"] = kwargs.get("timeout")
        return state["result"]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def test_expand_command_carries_target_and_waits_for_completion_by_default(
    skills_root, recorded, target
):
    recorded["result"] = subprocess.CompletedProcess(
        [], 0, json.dumps({"status": "ok", "replicas": {"before": 2, "after": 3}}), ""
    )
    runner = SkillRunner(skills_root, python="python-test")

    result = runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    command = recorded["command"]
    assert command[0] == "python-test"
    assert command[1].endswith("scale_up.py")
    assert command[2:4] == ["--kube-context", "test"]
    assert command[command.index("--replicas") + 1] == "3"
    # Waiting is the default: the script must poll until the operation is really done,
    # otherwise "finished" would only mean "the request was accepted".
    assert command[-3:] == ["--wait", "--wait-timeout", "600.0"]
    assert "--dry-run" not in command
    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.payload["replicas"] == {"before": 2, "after": 3}


def test_disabled_wait_is_reported_because_completion_is_then_unobserved(
    skills_root, recorded, target, caplog
):
    """With waiting off a script returns on acceptance, so completion is not observed."""
    runner = SkillRunner(skills_root, wait=False)

    runner.run(CATALOG["preload_cache"], {"paths": ["/models/a.bin"]}, target=target)

    assert "--wait" not in recorded["command"]
    assert "Waiting is disabled" in caplog.text


def test_wait_and_dry_run_flags_are_host_controlled(skills_root, recorded, target):
    runner = SkillRunner(skills_root, wait=True, wait_timeout_seconds=30.0, dry_run=True)

    runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    command = recorded["command"]
    assert command[command.index("--wait") + 1] == "--wait-timeout"
    assert command[command.index("--wait-timeout") + 1] == "30.0"
    assert command[-1] == "--dry-run"


def test_script_timeout_outlasts_the_wait_budget(skills_root, recorded, target):
    """A polling script must not be killed while it is still polling."""
    runner = SkillRunner(
        skills_root, timeout_seconds=300.0, wait=True, wait_timeout_seconds=600.0
    )

    runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    assert recorded["timeout"] == pytest.approx(660.0)


def test_timeout_is_left_alone_when_waiting_is_off(skills_root, recorded, target):
    runner = SkillRunner(
        skills_root, timeout_seconds=45.0, wait=False, wait_timeout_seconds=600.0
    )

    runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    assert recorded["timeout"] == pytest.approx(45.0)


def test_wait_is_not_passed_to_scripts_that_do_not_support_it(skills_root, recorded, target):
    runner = SkillRunner(skills_root, wait=True)

    runner.run(CATALOG["evict_cache"], {"count": 2, "path": "/models"}, target=target)

    assert "--wait" not in recorded["command"]


def test_missing_json_output_is_unknown_and_not_retried(skills_root, recorded, target):
    recorded["result"] = subprocess.CompletedProcess([], 0, "some log line\n", "")
    runner = SkillRunner(skills_root)

    result = runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    assert result.status == "unknown"
    assert "is unknown" in result.error


def test_reported_status_beats_the_exit_code(skills_root, recorded, target):
    recorded["result"] = subprocess.CompletedProcess(
        [], 1, json.dumps({"status": "partial", "action": "evict_lru"}), ""
    )
    runner = SkillRunner(skills_root)

    result = runner.run(CATALOG["evict_cache"], {"count": 2, "path": "/models"}, target=target)

    assert result.status == "partial"
    assert result.exit_code == 1


def test_timeout_reports_an_unknown_outcome(skills_root, monkeypatch, target):
    def raise_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    runner = SkillRunner(skills_root, timeout_seconds=5)

    result = runner.run(CATALOG["expand_cache"], {"desired_replicas": 3}, target=target)

    assert result.status == "timeout"
    assert "not retried" in result.error


def test_script_outside_the_skill_root_is_refused(skills_root, recorded, target):
    spec = replace(CATALOG["expand_cache"], script="../escape.py")
    runner = SkillRunner(skills_root)

    result = runner.run(spec, {"desired_replicas": 3}, target=target)

    assert result.status == "error"
    assert "escapes the skill directory" in result.error
    assert recorded["command"] is None


def test_missing_script_is_reported_as_an_error(skills_root, recorded, target):
    spec = replace(CATALOG["expand_cache"], script="fluid-cache-absent/scripts/none.py")
    runner = SkillRunner(skills_root)

    result = runner.run(spec, {"desired_replicas": 3}, target=target)

    assert result.status == "error"
    assert "not found" in result.error


def test_missing_skill_root_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        SkillRunner(tmp_path / "absent")
