"""Check target-bound metric tools using the existing reader and actual HTTP queries."""

from unittest.mock import Mock

import pytest
import requests

from server.agent.config import UserMetricConfig
from server.agent.schemas import Metric
from server.agent.tools import FLUID_METRICS, K8S_METRICS, create_metric_tools
from server.prometheus.prometheus_client import MetricsReader, PromQLReader


@pytest.mark.parametrize("source,names", [("fluid", FLUID_METRICS), ("k8s", K8S_METRICS)])
def test_metric_tool_returns_values_labels_and_timestamps(target, prometheus_server, source, names):
    tools = {
        tool.name: tool for tool in create_metric_tools(PromQLReader(prometheus_server.url), target)
    }

    result = tools[f"read_{source}_metrics"].invoke({})

    expected = prometheus_server.payloads[source]["data"]["result"]
    assert result["collector"] == source
    assert {metric["name"]: metric["value"] for metric in result["metrics"]} == {
        row["metric"]["__name__"]: float(row["value"][1]) for row in expected
    }
    assert not result["missing_metrics"]
    assert all(metric["timestamp_ms"] == 1234500 for metric in result["metrics"])
    assert len(prometheus_server.queries) == len(names)
    for query in prometheus_server.queries:
        assert 'namespace="default"' in query
        assert 'kube_context="test"' in query
        if source == "fluid":
            assert 'runtime="training"' in query
            assert 'dataset="training"' in query


@pytest.mark.parametrize("source", ["fluid", "k8s"])
def test_partial_result_keeps_other_metrics_and_lists_missing_values(
    target, prometheus_server, source
):
    rows = prometheus_server.payloads[source]["data"]["result"]
    missing = rows.pop()["metric"]["__name__"]
    rows[0]["value"][1] = "0"
    tools = {
        tool.name: tool for tool in create_metric_tools(PromQLReader(prometheus_server.url), target)
    }

    result = tools[f"read_{source}_metrics"].invoke({})

    assert result["missing_metrics"] == [missing]
    assert result["metrics"][0]["value"] == 0
    assert all(metric["name"] != missing for metric in result["metrics"])
    assert any(missing in warning for warning in result["warnings"])


def test_all_missing_values_are_reported_without_synthetic_samples(target):
    reader = Mock(spec=MetricsReader)
    reader.current.return_value = []
    tool = create_metric_tools(reader, target)[0]

    result = tool.invoke({})

    assert result["metrics"] == []
    assert result["missing_metrics"] == list(FLUID_METRICS)


def test_http_failure_is_not_mistaken_for_missing_values(target, prometheus_server):
    prometheus_server.status_code = 503
    tool = create_metric_tools(PromQLReader(prometheus_server.url), target)[0]

    with pytest.raises(requests.HTTPError, match="503"):
        tool.invoke({})


def test_prometheus_query_error_propagates(target, prometheus_server):
    prometheus_server.payloads["fluid"] = {"status": "error", "error": "query failed"}
    tool = create_metric_tools(PromQLReader(prometheus_server.url), target)[0]

    with pytest.raises(RuntimeError, match="query failed"):
        tool.invoke({})


@pytest.mark.parametrize("fault", ["other_target", "wrong_name", "non_finite"])
def test_invalid_reader_sample_is_rejected(target, fault):
    reader = Mock(spec=MetricsReader)
    sample = Metric(
        name=FLUID_METRICS[0],
        help="Cache capacity",
        value=1024,
        labels={
            "kube_context": target.kube_context,
            "namespace": target.namespace,
            "dataset": target.dataset,
            "runtime": target.runtime_name,
            "runtime_plural": target.runtime_plural,
        },
    )
    if fault == "other_target":
        sample.labels["namespace"] = "another-namespace"
    elif fault == "wrong_name":
        sample.name = "other_metric"
    else:
        sample.value = float("nan")
    reader.current.return_value = [sample]

    with pytest.raises(RuntimeError):
        create_metric_tools(reader, target)[0].invoke({})


def test_query_selector_escapes_quotes_backslashes_and_newlines():
    selector = PromQLReader._selector("fluid_workers", {"namespace": 'a"b\\c\nd'})

    assert selector == 'fluid_workers{namespace="a\\"b\\\\c\\nd"}'


def test_user_system_tool_reads_without_target_labels(target):
    reader = Mock(spec=MetricsReader)
    reader.current.side_effect = [
        [Metric(name="model_file_access_count", help="total", value=3.0,
                labels={"source": "hdfs", "file": "a.bin"})],
        [],  # model_download_bandwidth has no sample this round
    ]
    descriptors = (
        UserMetricConfig(name="model_file_access_count", help="total",
                         labels=("file",), kind="counter"),
        UserMetricConfig(name="model_download_bandwidth", help="bandwidth"),
    )

    tools = create_metric_tools(reader, target, descriptors)
    assert tools[-1].name == "read_user_system_metrics"

    result = tools[-1].invoke({})

    assert result["collector"] == "user_system"
    assert result["identity"] == {}
    assert result["metrics"][0]["value"] == 3.0
    assert result["metrics"][0]["labels"] == {"source": "hdfs", "file": "a.bin"}
    assert result["missing_metrics"] == ["model_download_bandwidth"]
    # User-system metrics are unrelated to the cluster: reads must not filter by target labels
    assert reader.current.call_args.args[1] == {}


def test_user_system_tool_absent_when_not_configured(target):
    tools = create_metric_tools(Mock(spec=MetricsReader), target)

    assert [tool.name for tool in tools] == ["read_fluid_metrics", "read_k8s_metrics"]


def test_user_system_tool_reports_missing_series(target):
    reader = Mock(spec=MetricsReader)
    reader.current.return_value = []
    descriptors = (UserMetricConfig(name="model_file_size_bytes", help="size"),)

    result = create_metric_tools(reader, target, descriptors)[-1].invoke({})

    assert result["missing_metrics"] == ["model_file_size_bytes"]
