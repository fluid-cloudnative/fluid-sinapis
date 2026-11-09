"""Load Kubernetes connection settings for resource-specific adapters."""

import os

from kubernetes import client, config
from requests.utils import should_bypass_proxies


def bypass_no_proxy(configuration: client.Configuration) -> None:
    """Some kubernetes client versions read https_proxy into Configuration but clear
    no_proxy, so internal cluster addresses get hijacked by the proxy (connection cut,
    SSL EOF). Re-evaluate with the environment's no_proxy semantics: matching hosts
    always connect directly."""
    if not configuration.proxy:
        return
    if should_bypass_proxies(configuration.host,
                             no_proxy=os.environ.get("NO_PROXY")
                             or os.environ.get("no_proxy")):
        configuration.proxy = None


class KubernetesAdapter:
    """Provide a custom-resource API client and request timeout to subclasses."""

    def __init__(
        self,
        kube_context: str,
        *,
        request_timeout_seconds: float = 10,
        api: client.CustomObjectsApi | None = None,
    ) -> None:
        """Load the named kubeconfig context unless a configured API is supplied."""
        if api is None:
            if not kube_context.strip():
                raise ValueError("kube_context must not be empty")
            configuration = client.Configuration()
            config.load_kube_config(context=kube_context, client_configuration=configuration)
            bypass_no_proxy(configuration)
            api = client.CustomObjectsApi(client.ApiClient(configuration))
        self.api = api
        self.timeout = request_timeout_seconds
