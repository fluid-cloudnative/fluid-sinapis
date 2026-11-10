"""config.yml loading: precedence env > yml > code defaults, with clear errors on bad values."""

import pytest

from server.agent.schemas import Target
from server.config import DEFAULT_TARGET, AppConfig, K8sSettings


def test_missing_file_falls_back_to_defaults(tmp_path):
    cfg = AppConfig.load(path=tmp_path / "nope.yml", env={})

    # DEFAULT_TARGET keeps runtime_name empty as the merge template; a fully built
    # config resolves it against the dataset, so compare against the validated form.
    assert cfg.target == Target(**DEFAULT_TARGET.model_dump())
    assert cfg.k8s == K8sSettings()
    assert cfg.prometheus.pushgateway_url == "http://localhost:9091"
    assert cfg.prometheus.query_url == "http://localhost:9090"
    assert cfg.collectors.interval_seconds == 10.0


def test_yaml_values_win_over_defaults(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """
target:
  kube_context: ctx
  namespace: other-ns
  dataset: resnet
  runtime_plural: thunderxruntimes
k8s:
  request_timeout_seconds: 3.5
prometheus:
  pushgateway_url: http://prom:9091
  query_url: http://prom:9090
collectors:
  interval_seconds: 30
""",
        encoding="utf-8",
    )

    cfg = AppConfig.load(path=path, env={})

    assert cfg.target.kube_context == "ctx"
    assert cfg.target.namespace == "other-ns"
    assert cfg.target.dataset == "resnet"
    assert cfg.target.runtime_plural == "thunderxruntimes"
    assert cfg.k8s.request_timeout_seconds == 3.5
    assert cfg.prometheus.pushgateway_url == "http://prom:9091"
    assert cfg.prometheus.query_url == "http://prom:9090"
    assert cfg.collectors.interval_seconds == 30.0


def test_env_overrides_yaml(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("target:\n  namespace: from-yml\n", encoding="utf-8")

    cfg = AppConfig.load(path=path, env={"KUBE_NS": "from-env"})

    assert cfg.target.namespace == "from-env"


def test_runtime_name_follows_dataset_when_unset(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("target:\n  dataset: bert\n", encoding="utf-8")

    assert AppConfig.load(path=path, env={}).resolved_target().runtime_name == "bert"

    explicit = AppConfig.load(path=path, env={"FLUID_RUNTIME": "explicit"})

    assert explicit.resolved_target().runtime_name == "explicit"


def test_collector_interval_can_be_overridden_by_env(tmp_path):
    """interval_seconds used to have no env mapping, so it could only be set in the file."""
    path = tmp_path / "config.yml"
    path.write_text("collectors:\n  interval_seconds: 30\n", encoding="utf-8")

    cfg = AppConfig.load(path=path, env={"COLLECTOR_INTERVAL_SECONDS": "5"})

    assert cfg.collectors.interval_seconds == 5.0


def test_prometheus_query_url_can_be_overridden_by_env(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("prometheus:\n  query_url: http://yml:9090\n", encoding="utf-8")

    cfg = AppConfig.load(path=path, env={"PROMETHEUS_URL": "http://env:9090"})

    assert cfg.prometheus.query_url == "http://env:9090"


def test_free_mode_only_accepts_request_or_usage(tmp_path):
    """A wrong value used to degrade silently to request; now it fails loudly."""
    path = tmp_path / "config.yml"

    path.write_text("k8s:\n  free_mode: usage\n", encoding="utf-8")
    assert AppConfig.load(path=path, env={}).k8s.free_mode == "usage"

    path.write_text("k8s:\n  free_mode: nonsense\n", encoding="utf-8")
    with pytest.raises(ValueError, match="k8s.free_mode"):
        AppConfig.load(path=path, env={})


def test_invalid_value_reports_section_and_key(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("k8s:\n  request_timeout_seconds: ten\n", encoding="utf-8")

    with pytest.raises(ValueError, match="k8s.request_timeout_seconds"):
        AppConfig.load(path=path, env={})


@pytest.mark.parametrize(
    "body,where",
    [
        ("collectors:\n  interval_seconds: 0\n", "collectors.interval_seconds"),
        ("k8s:\n  request_timeout_seconds: -1\n", "k8s.request_timeout_seconds"),
        (
            "prometheus:\n  request_timeout_seconds: 0\n",
            "prometheus.request_timeout_seconds",
        ),
    ],
)
def test_non_positive_durations_are_rejected(tmp_path, body, where):
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=where):
        AppConfig.load(path=path, env={})


@pytest.mark.parametrize(
    "body,moved_to",
    [
        ("k8s:\n  namespace: old-ns\n", "target.namespace"),
        ("k8s:\n  kube_context: old-ctx\n", "target.kube_context"),
        ("fluid:\n  dataset: old-dataset\n", "target.dataset"),
        (
            "prometheus:\n  prometheus_query_url: http://old:9090\n",
            "prometheus.query_url",
        ),
        ("metrics:\n  prometheus_url: http://old:9090\n", "prometheus.query_url"),
        ("metrics:\n  request_timeout_seconds: 3\n", "prometheus.request_timeout_seconds"),
    ],
)
def test_migrated_keys_fail_loudly_with_the_new_location(tmp_path, body, moved_to):
    """A legacy key left in the file would fail silently, so failing at startup is safer."""
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=moved_to):
        AppConfig.load(path=path, env={})


def test_resolved_target_is_complete_for_the_collectors(tmp_path):
    """Collectors need all five target fields; supplying only two once caused a
    ValidationError at startup."""
    path = tmp_path / "config.yml"
    path.write_text("target:\n  dataset: ds\n  runtime_name: ''\n", encoding="utf-8")

    target = AppConfig.load(path=path, env={}).resolved_target()

    assert target.dataset == "ds"
    assert target.runtime_name == "ds"
    assert target.runtime_plural
    assert target.kube_context and target.namespace


def test_real_repo_config_parses_consistently():
    """The repository's own config.yml must parse and must not conflict with the code defaults."""
    cfg = AppConfig.load(env={})

    assert cfg.target.kube_context == DEFAULT_TARGET.kube_context
    assert cfg.target.namespace == DEFAULT_TARGET.namespace
    assert cfg.target.runtime_plural == DEFAULT_TARGET.runtime_plural
    assert cfg.k8s == K8sSettings()
    assert cfg.prometheus.pushgateway_url == "http://localhost:9091"
    assert cfg.prometheus.query_url == "http://localhost:9090"
    # An explicitly given runtime_name must not be overwritten by dataset
    assert cfg.resolved_target().runtime_name == cfg.target.runtime_name
