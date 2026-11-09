"""Verify model settings and configuration forwarding at the runtime entrypoint."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent.config import ModelConfig, RuntimeConfig
from server.agent.run_agents import create_model, main


def test_model_client_uses_configured_connection_and_generation_settings():
    config = ModelConfig(
        name="test-model",
        api_key="test-secret",
        base_url="https://model.example/v1",
        timeout_seconds=12,
        temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
    )

    model = create_model(config)

    assert model.model_name == "test-model"
    assert model.openai_api_key.get_secret_value() == "test-secret"
    assert model.openai_api_base == "https://model.example/v1"
    assert model.request_timeout == 12
    assert model.temperature == 0.2
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert model.max_retries == 0


@pytest.fixture
def config(target, tmp_path, monkeypatch):
    # Turn off setup_logging's file output and detach the handler it added to root
    # afterwards
    monkeypatch.setenv("AGENT_LOG_FILE", "off")
    yield RuntimeConfig(
        planner_model={"name": "planner", "api_key": "test"},
        validator_model={"name": "validator", "api_key": "test"},
        agent={"interval_seconds": 5},
        target=target,
        skills_dir=tmp_path,
        operation_skills_dir=tmp_path,
    )
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_sinapis_agent", False):
            root.removeHandler(handler)


def test_main_uses_the_selected_config_and_single_round_flag(config):
    with (
        patch("sys.argv", ["run_agents", "--config", "custom.yml", "--once"]),
        patch("server.agent.run_agents.load_config", return_value=config) as load,
        patch("server.agent.run_agents.build_workflow") as build,
        patch("server.agent.run_agents.run_loop") as loop,
    ):
        main()

    load.assert_called_once_with("custom.yml")
    build.assert_called_once_with(config)
    loop.assert_called_once_with(build.return_value, config, once=True)


def test_main_defaults_to_server_config_from_another_directory(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with (
        patch("sys.argv", ["run_agents", "--once"]),
        patch("server.agent.run_agents.load_config", return_value=config) as load,
        patch("server.agent.run_agents.build_workflow"),
        patch("server.agent.run_agents.run_loop"),
    ):
        main()

    expected = Path(__file__).resolve().parents[3] / "server/config.yml"
    load.assert_called_once_with(expected)
