"""Run one fluid_skills script as a subprocess and classify its outcome.

The convention matches the skill documentation: the script prints a single JSON line
to stdout, logs to stderr, and exits 0/1/2. This module only does "run once, classify
faithfully" and never retries: for network-type failures the submission result is
unknown and a retry could submit twice, so whether to continue is the orchestrator's
decision based on status.
"""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from server.agent.schemas import Target

from .catalog import ToolSpec, build_arguments

logger = logging.getLogger(__name__)

RunStatus = Literal["ok", "partial", "error", "timeout", "unknown"]

_REPORTED_STATUSES = {"ok", "partial", "error", "timeout", "unknown"}


@dataclass(frozen=True)
class SkillRun:
    """One script invocation's observed result."""

    status: RunStatus
    exit_code: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    command: tuple[str, ...] = ()


class SupportsSkillRun(Protocol):
    """Anything the executor can hand a validated call to."""

    def run(
        self, spec: ToolSpec, arguments: dict[str, Any], *, target: Target
    ) -> SkillRun: ...


class SkillRunner:
    """Execute catalog tools with a fixed interpreter, timeout, and path root."""

    def __init__(
        self,
        skills_root: str | Path,
        *,
        python: str | None = None,
        # The default pair mirrors server/config.yml: the script timeout must outlast
        # the wait budget so a polling script is never killed mid-poll.
        timeout_seconds: float = 660.0,
        wait: bool = True,
        wait_timeout_seconds: float = 600.0,
        dry_run: bool = False,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(skills_root).resolve()
        if not self._root.is_dir():
            raise ValueError(f"operation skill directory does not exist: {self._root}")
        self._python = python or sys.executable
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._wait = wait
        self._wait_timeout = wait_timeout_seconds
        # The config layer (ExecutionConfig) rejects an inconsistent timeout pair at
        # startup; here a programmatic inconsistency fails loudly too, instead of
        # being silently rewritten.
        if wait and timeout_seconds <= wait_timeout_seconds:
            raise ValueError(
                "timeout_seconds must stay above wait_timeout_seconds so a script "
                f"polling for completion is not killed mid-poll "
                f"(got timeout_seconds={timeout_seconds:g}, "
                f"wait_timeout_seconds={wait_timeout_seconds:g})"
            )
        self._timeout = timeout_seconds
        self._dry_run = dry_run
        self._extra_env = dict(extra_env or {})

    def run(
        self, spec: ToolSpec, arguments: dict[str, Any], *, target: Target
    ) -> SkillRun:
        """Invoke `spec` once and classify the outcome; never retries."""
        try:
            script = self._resolve_script(spec)
            command = [self._python, str(script), *build_arguments(spec, arguments, target)]
            command += list(spec.flags)
            if self._wait and spec.wait_supported:
                command += ["--wait", "--wait-timeout", str(self._wait_timeout)]
            elif spec.wait_supported:
                # Without --wait the script returns once the request is accepted, so any
                # "finished" report downstream means "submitted", not "done".
                logger.warning(
                    "Waiting is disabled: %s returns as soon as the request is accepted, "
                    "so its real completion is not observed", spec.skill,
                )
            if self._dry_run:
                command.append("--dry-run")
        except Exception as exc:  # noqa: BLE001 - parameter/path problems must reach the caller
            return SkillRun(status="error", error=f"{exc.__class__.__name__}: {exc}")

        logger.info("Running skill %s: %s", spec.skill, " ".join(command))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
                env={**self._environment()},
            )
        except subprocess.TimeoutExpired:
            # The child process has been killed after the timeout, but the request may
            # already be submitted: the result is unknown, so do not retry.
            return SkillRun(
                status="timeout",
                error=(
                    f"script did not return within {self._timeout:g}s; the outcome is unknown "
                    "and it was not retried"
                ),
                command=tuple(command),
            )
        except OSError as exc:
            return SkillRun(
                status="error",
                error=f"could not start the script: {exc.__class__.__name__}: {exc}",
                command=tuple(command),
            )

        payload = _last_json_object(completed.stdout)
        if payload is None:
            return SkillRun(
                status="unknown",
                exit_code=completed.returncode,
                error=(
                    "script printed no parseable JSON, so whether it was submitted is unknown: "
                    f"stdout={_tail(completed.stdout)} stderr={_tail(completed.stderr)}"
                ),
                command=tuple(command),
            )
        reported = payload.get("status")
        status = _status(payload, completed.returncode)
        error = payload.get("error") if reported not in (None, "ok") else None
        if completed.returncode != 0 and reported in (None, "ok", "partial"):
            # Keep the conflicting payload visible: the process failure wins, but the
            # reported status is worth recording for debugging.
            error = (
                f"script exited with {completed.returncode} but reported "
                f"status={reported!r}; treating the outcome as {status}"
            )
        return SkillRun(
            status=status,
            exit_code=completed.returncode,
            payload=payload,
            error=error,
            command=tuple(command),
        )

    def _resolve_script(self, spec: ToolSpec) -> Path:
        """Keep script execution inside the configured skill root."""
        script = (self._root / spec.script).resolve()
        if not script.is_relative_to(self._root):
            raise ValueError(f"script path escapes the skill directory: {spec.script}")
        if not script.is_file():
            raise FileNotFoundError(f"skill script not found: {script}")
        return script

    def _environment(self) -> dict[str, str]:
        # Inherit the current environment (KUBECONFIG/PATH, etc.), then layer the
        # explicit settings on top.
        return {**os.environ, **self._extra_env}


def _status(payload: dict[str, Any], exit_code: int) -> RunStatus:
    """Classify the outcome; a nonzero exit code always wins over a reported status.

    A script that reports ok but exits nonzero may have died after printing its
    payload and before finishing its work, so its "ok" cannot be trusted: treat the
    outcome as unknown (a definite error/timeout report is still kept as-is).
    """
    reported = payload.get("status")
    if exit_code != 0:
        if isinstance(reported, str) and reported in ("error", "timeout", "unknown"):
            return reported  # type: ignore[return-value]
        return "unknown"
    if isinstance(reported, str) and reported in _REPORTED_STATUSES:
        return reported  # type: ignore[return-value]
    return "ok"


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    """Pick the last stdout line that parses as a JSON object."""
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tail(text: str, limit: int = 400) -> str:
    return text.strip()[-limit:]
