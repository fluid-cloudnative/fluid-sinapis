"""Shared model doubles, target resources, and a local Prometheus HTTP fixture."""

import json
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from server.agent.schemas import Target


class ToolCallingModel(GenericFakeChatModel):
    """Record model inputs while replaying deterministic responses through agent graphs."""

    requests: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ToolCallingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.requests.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def model_factory():
    """Create a model that can call tools, return plain text, or fail at a selected stage."""

    def make(*responses: tuple[str, dict[str, Any]] | AIMessage | Exception) -> ToolCallingModel:
        def messages() -> Iterator[AIMessage]:
            for index, response in enumerate(responses):
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, AIMessage):
                    yield response
                    continue
                name, args = response
                yield AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": args, "id": f"call-{index}"}],
                )

        return ToolCallingModel(messages=messages())

    return make


@pytest.fixture
def target():
    return Target(
        kube_context="test",
        namespace="default",
        dataset="training",
        runtime_name="training",
        runtime_plural="alluxioruntimes",
    )


@pytest.fixture
def prometheus_server(target, monkeypatch):
    """Serve known vectors through the actual PromQLReader HTTP implementation."""
    labels = {"kube_context": target.kube_context, "namespace": target.namespace}
    fluid_labels = {
        **labels,
        "runtime": target.runtime_name,
        "dataset": target.dataset,
        "runtime_plural": target.runtime_plural,
    }
    data = {
        "fluid": (
            {
                "fluid_cache_capacity_bytes": 10240,
                "fluid_cache_capacity_per_worker_bytes": 5120,
                "fluid_workers": 2,
                "fluid_workers_desired": 2,
                "fluid_cache_used_bytes": 2048,
                "fluid_cache_used_ratio": 0.2,
            },
            fluid_labels,
        ),
        "k8s": (
            {
                "k8s_nodes": 3,
                "k8s_node_allocatable_memory_bytes": 32768,
                "k8s_ready_pods": 4,
                "k8s_free_memory_bytes": 16384,
            },
            labels,
        ),
    }
    payloads = {
        source: {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": name, **series_labels}, "value": [1234.5, str(value)]}
                    for name, value in values.items()
                ],
            },
        }
        for source, (values, series_labels) in data.items()
    }
    state = SimpleNamespace(payloads=payloads, queries=[], status_code=200, url="")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/v1/query":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)["query"][0]
            state.queries.append(query)
            source = "fluid" if "fluid_" in query else "k8s"
            payload = state.payloads[source]
            if payload.get("status") == "success":
                # Each reader.current call requests one metric by its exact name.
                payload = {
                    **payload,
                    "data": {
                        **payload["data"],
                        "result": [
                            row
                            for row in payload["data"]["result"]
                            if query.startswith("last_over_time(" + row["metric"]["__name__"] + "{")
                        ],
                    },
                }
            body = json.dumps(payload).encode()
            self.send_response(state.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        state.url = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()
