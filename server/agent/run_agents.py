"""Run periodic planning, validation, and execution agent workflow."""

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from langchain_openai import ChatOpenAI

from server.agent.config import ModelConfig, RuntimeConfig, load_config
from server.agent.execution import PlanExecutor, SkillRunner
from server.agent.llm_logging import LLMCallLogger
from server.agent.tools import create_metric_tools
from server.agent.workflow import AgentWorkflow, run_loop
from server.prometheus.prometheus_client import PromQLReader

logger = logging.getLogger(__name__)


def create_model(config: ModelConfig, role: str = "") -> ChatOpenAI:
    """Create each agent's model client from its resolved connection settings.

    ``role`` labels this client's blocks in the LLM-call log (planner / validator).
    """
    return ChatOpenAI(
        model=config.name,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        timeout=config.timeout_seconds,
        max_retries=0,
        extra_body=config.extra_body,
        callbacks=[
            LLMCallLogger(
                config.name,
                role=role,
                params={
                    "temperature": config.temperature,
                    "timeout_seconds": config.timeout_seconds,
                    "extra_body": config.extra_body,
                },
            )
        ],
    )


def build_workflow(config: RuntimeConfig) -> AgentWorkflow:
    """Wire independent models, planning skills, metrics, and the operation executor."""
    reader = PromQLReader(
        config.prometheus.query_url, timeout=config.prometheus.request_timeout_seconds
    )
    executor = PlanExecutor(
        target=config.target,
        runner=SkillRunner(
            config.operation_skills_dir,
            timeout_seconds=config.execution.timeout_seconds,
            wait=config.execution.wait,
            wait_timeout_seconds=config.execution.wait_timeout_seconds,
            dry_run=config.execution.dry_run,
        ),
        allowed_path_prefixes=config.execution.allowed_path_prefixes,
    )
    return AgentWorkflow(
        planner_model=create_model(config.planner_model, role="planner"),
        validator_model=create_model(config.validator_model, role="validator"),
        executor=executor,
        tools=create_metric_tools(reader, config.target, config.user_system.metrics),
        skills_dir=config.skills_dir,
        review_skills_dir=config.review_skills_dir or config.skills_dir,
        user_skills_dir=config.user_skills_dir,
        validation_enabled=config.agent.validation_enabled,
    )


_HANDLER_MARK = "_sinapis_agent"


def setup_logging(level: int = logging.INFO) -> None:
    """Write stage logs twice: stderr (console / script redirect) and a rotating
    logs/agent.log file.

    The file path can be overridden with the ``AGENT_LOG_FILE`` environment variable;
    set it to ``off`` to disable file output. Repeated calls do not stack handlers
    (they are marked for de-duplication, so tests may call main safely).
    """
    root = logging.getLogger()
    root.setLevel(level)
    if any(getattr(handler, _HANDLER_MARK, False) for handler in root.handlers):
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _HANDLER_MARK, True)
    root.addHandler(console)

    raw = os.environ.get("AGENT_LOG_FILE", "").strip()
    if raw.lower() in {"off", "none"}:
        return
    path = Path(raw) if raw else Path(__file__).resolve().parents[2] / "logs" / "agent.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARK, True)
    root.addHandler(file_handler)


def main() -> None:
    """Load configuration once and run until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    # Anchor the default to this module so it also works outside the repository root.
    parser.add_argument(
        "--config",
        default=Path(__file__).resolve().parents[1] / "config.yml",
        help="Path to runtime YAML settings (default: server/config.yml)",
    )
    parser.add_argument("--once", action="store_true", help="Run a single agent workflow round")
    args = parser.parse_args()
    setup_logging()
    config = load_config(args.config)
    try:
        workflow = build_workflow(config)
        run_loop(workflow, config, once=args.once)
    except KeyboardInterrupt:
        logger.info("Agent loop stopped")


if __name__ == "__main__":
    main()
