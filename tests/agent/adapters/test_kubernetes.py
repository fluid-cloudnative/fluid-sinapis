"""Verify kubeconfig loading and API injection without contacting a cluster."""

from unittest.mock import Mock, patch

import pytest
from kubernetes import client

from server.agent.adapters.kubernetes import KubernetesAdapter


def test_named_context_creates_an_isolated_client():
    with (
        patch("server.agent.adapters.kubernetes.config.load_kube_config") as load_config,
        patch("server.agent.adapters.kubernetes.client.ApiClient") as api_client,
        patch("server.agent.adapters.kubernetes.client.CustomObjectsApi") as custom_api,
    ):
        adapter = KubernetesAdapter("test-context")

    configuration = load_config.call_args.kwargs["client_configuration"]
    assert load_config.call_args.kwargs["context"] == "test-context"
    api_client.assert_called_once_with(configuration)
    custom_api.assert_called_once_with(api_client.return_value)
    assert adapter.api is custom_api.return_value


def test_injected_api_skips_kubeconfig_loading():
    api = Mock(spec=client.CustomObjectsApi)
    with patch("server.agent.adapters.kubernetes.config.load_kube_config") as load_config:
        adapter = KubernetesAdapter("test", api=api, request_timeout_seconds=7)

    load_config.assert_not_called()
    assert adapter.api is api
    assert adapter.timeout == 7


def test_blank_context_does_not_silently_use_the_default_cluster():
    with patch("server.agent.adapters.kubernetes.config.load_kube_config") as load_config:
        with pytest.raises(ValueError, match="kube_context"):
            KubernetesAdapter(" ")
    load_config.assert_not_called()
