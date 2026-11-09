"""Validate YAML model overrides and paths without contacting external services."""

import pytest
import yaml
from pydantic import ValidationError

from server.agent.config import load_config


@pytest.fixture
def config_data(target):
    return {
        "model": {
            "name": "default-model",
            "api_key": "test-secret",
            "base_url": "https://model.example/v1",
        },
        "agent": {"interval_seconds": 15},
        "target": target.model_dump(),
        "skills": {"official_dir": "skills"},
        "unrelated_application_section": {"enabled": True},
    }


def write_config(tmp_path, data):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_role_overrides_inherit_shared_connection_settings(tmp_path, config_data):
    config_data["models"] = {
        "planner": {"name": "planner-model"},
        "validator": {"name": "review-model", "api_key": "review-secret"},
    }

    config = load_config(write_config(tmp_path, config_data))

    assert config.planner_model.name == "planner-model"
    assert config.validator_model.name == "review-model"
    assert config.planner_model.api_key.get_secret_value() == "test-secret"
    assert config.validator_model.api_key.get_secret_value() == "review-secret"
    assert config.planner_model.base_url == config.validator_model.base_url
    assert config.skills_dir == tmp_path / "skills"
    assert "test-secret" not in repr(config)
    assert "review-secret" not in repr(config)


def test_shared_model_can_be_used_without_role_overrides(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))

    assert config.planner_model == config.validator_model
    assert config.agent.validation_enabled is True


def test_user_system_section_yields_observation_metrics(tmp_path, config_data):
    config_data["user_system"] = {
        "metrics": [{"name": "model_download_bandwidth", "help": "bandwidth", "type": "gauge"}],
        "events": [{
            "name": "model_file_access",
            "help": "model file access events",
            "labels": ["file"],
            "counter": "model_file_access_count",
            "gauges": [
                {"field": "access_time", "metric": "model_file_last_access_time",
                 "help": "last access time"},
                {"field": "size_bytes", "metric": "model_file_size_bytes",
                 "help": "file size"},
            ],
        }],
    }

    config = load_config(write_config(tmp_path, config_data))

    descriptors = config.user_system.metrics
    assert [(m.name, m.kind) for m in descriptors] == [
        ("model_download_bandwidth", "gauge"),
        ("model_file_access_count", "counter"),
        ("model_file_last_access_time", "gauge"),
        ("model_file_size_bytes", "gauge"),
    ]
    assert descriptors[1].labels == ("file",)
    assert descriptors[2].labels == ("file",)


def test_user_system_defaults_to_empty_without_section(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))

    assert config.user_system.metrics == ()


def test_review_dir_defaults_to_official_dir(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))

    assert config.review_skills_dir == config.skills_dir
    assert config.skills_dir == tmp_path / "skills"


def test_review_dir_resolves_relative_to_config(tmp_path, config_data):
    config_data["skills"]["review_dir"] = "review"
    config_data["skills"]["official_dir"] = "official"

    config = load_config(write_config(tmp_path, config_data))

    assert config.skills_dir == tmp_path / "official"
    assert config.review_skills_dir == tmp_path / "review"


def test_user_dir_defaults_and_resolves_relative_to_config(tmp_path, config_data):
    default = load_config(write_config(tmp_path, config_data))
    assert default.user_skills_dir == tmp_path / "agent/skills/user_skills"

    config_data["skills"]["user_dir"] = "custom-strategies"
    config = load_config(write_config(tmp_path, config_data))
    assert config.user_skills_dir == tmp_path / "custom-strategies"


def test_duplicate_user_system_derived_names_are_rejected(tmp_path, config_data):
    config_data["user_system"] = {
        "metrics": [
            {"name": "model_file_access_count", "help": "collides with the event-derived metric"}
        ],
        "events": [{
            "name": "model_file_access", "help": "e", "labels": ["file"],
            "counter": "model_file_access_count", "gauges": [],
        }],
    }

    with pytest.raises(ValueError, match="duplicate derived metric name"):
        load_config(write_config(tmp_path, config_data))


def test_default_skill_path_is_relative_to_server_config(tmp_path, config_data):
    del config_data["skills"]
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    config = load_config(write_config(server_dir, config_data))

    assert config.skills_dir == server_dir / "agent/skills/official"


def test_skill_specific_policies_are_forwarded_without_an_action_registry(tmp_path, config_data):
    config_data["policies"] = {"new-operation": {"custom_limit": 4}}

    config = load_config(write_config(tmp_path, config_data))

    assert config.policies == {"new-operation": {"custom_limit": 4}}


def test_prometheus_endpoint_and_timeout_come_from_yaml(tmp_path, config_data):
    config_data["prometheus"] = {
        "query_url": "http://metrics.example:9090",
        "request_timeout_seconds": 4,
    }

    config = load_config(write_config(tmp_path, config_data))

    assert config.prometheus.query_url == "http://metrics.example:9090"
    assert config.prometheus.request_timeout_seconds == 4


def test_prometheus_endpoint_can_be_overridden_by_env(tmp_path, config_data, monkeypatch):
    """The read address shares the prometheus section, and environment variables can
    override both ends at once."""
    config_data["prometheus"] = {"query_url": "http://from-yml:9090"}
    monkeypatch.setenv("PROMETHEUS_URL", "http://from-env:9090")
    monkeypatch.setenv("PROMETHEUS_TIMEOUT_SECONDS", "7")

    config = load_config(write_config(tmp_path, config_data))

    assert config.prometheus.query_url == "http://from-env:9090"
    assert config.prometheus.request_timeout_seconds == 7


def test_prometheus_timeout_must_be_positive(tmp_path, config_data):
    field = "request_timeout_seconds"
    config_data["prometheus"] = {field: 0}

    with pytest.raises(ValidationError, match=field):
        load_config(write_config(tmp_path, config_data))


def test_api_key_can_come_from_an_environment_variable(tmp_path, config_data, monkeypatch):
    """Secrets come from the environment only; config.yml holds ${VAR}."""
    monkeypatch.setenv("TEST_MODEL_KEY", "from-env-secret")
    config_data["model"]["api_key"] = "${TEST_MODEL_KEY}"

    config = load_config(write_config(tmp_path, config_data))

    assert config.planner_model.api_key.get_secret_value() == "from-env-secret"
    assert config.validator_model.api_key.get_secret_value() == "from-env-secret"


def test_missing_api_key_variable_is_reported_by_name(tmp_path, config_data, monkeypatch):
    monkeypatch.delenv("ABSENT_MODEL_KEY", raising=False)
    config_data["model"]["api_key"] = "${ABSENT_MODEL_KEY}"

    with pytest.raises(ValueError, match="ABSENT_MODEL_KEY"):
        load_config(write_config(tmp_path, config_data))


def test_migrated_metrics_section_is_rejected(tmp_path, config_data):
    """The metrics section was merged into prometheus; keeping the old keys would
    silently do nothing, so loading fails fast instead."""
    config_data["metrics"] = {"prometheus_url": "http://old:9090"}

    with pytest.raises(ValueError, match="prometheus.query_url"):
        load_config(write_config(tmp_path, config_data))


@pytest.mark.parametrize("interval", [0, -1, "15"])
def test_invalid_intervals_are_rejected_before_runtime_start(tmp_path, config_data, interval):
    config_data["agent"]["interval_seconds"] = interval

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, config_data))


def test_model_errors_do_not_include_credentials(tmp_path, config_data):
    config_data["model"]["temperature"] = "invalid"

    with pytest.raises(ValidationError) as raised:
        load_config(write_config(tmp_path, config_data))

    assert "test-secret" not in str(raised.value)
    assert "temperature" in str(raised.value)


def test_runtime_type_must_be_explicit_in_config(tmp_path, config_data):
    del config_data["target"]["runtime_plural"]

    with pytest.raises(ValidationError, match="runtime_plural"):
        load_config(write_config(tmp_path, config_data))


@pytest.mark.parametrize("section", ["model", "models", "skills"])
def test_mapping_sections_reject_other_yaml_types(tmp_path, config_data, section):
    config_data[section] = []

    with pytest.raises(ValueError, match=section):
        load_config(write_config(tmp_path, config_data))


def test_unknown_model_role_is_rejected(tmp_path, config_data):
    config_data["models"] = {"reviewer": {"name": "typo"}}

    with pytest.raises(ValueError, match="planner and validator"):
        load_config(write_config(tmp_path, config_data))


def test_misspelled_skill_setting_is_rejected(tmp_path, config_data):
    config_data["skills"] = {"offical_dir": "skills"}

    with pytest.raises(ValueError, match="skills only accepts official_dir"):
        load_config(write_config(tmp_path, config_data))


def test_top_level_yaml_must_be_a_mapping(tmp_path):
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(write_config(tmp_path, []))
