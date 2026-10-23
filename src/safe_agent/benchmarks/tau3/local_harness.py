"""Scoped loopback transport for the unchanged pinned synchronous LLM harness."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from safe_agent.contracts.episode_inference import validate_local_api_base

from .config import BenchmarkConfig


@contextmanager
def harness_transport(config: BenchmarkConfig) -> Iterator[None]:
    """Keep the local route on its declared endpoint, with no cached old client.

    The official harness still creates messages, tools, and rewards. Only its
    synchronous HTTP transport is scoped here. The CLI runs one benchmark at a
    time, with the already enforced concurrency of one. Hosted runs retain the
    existing official transport behavior.
    """
    if config.model_route != "local":
        yield
        return
    if config.local_api_base is None:
        raise ValueError("LOCAL_API_BASE_REQUIRED")
    base = validate_local_api_base(config.local_api_base)

    import httpx
    import litellm
    from litellm.caching.llm_caching_handler import LLMClientCache

    # Import first: the pinned module initializes LiteLLM's shared clients.
    from tau2.utils import llm_utils  # type: ignore[import-untyped]  # noqa: F401

    expected_url = httpx.URL(base + "/chat/completions")

    class LocalTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.transport = httpx.HTTPTransport(retries=0, trust_env=False)

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if (request.method != "POST" or request.url != expected_url
                    or request.headers.get("authorization") != "Bearer safe-local-no-key"):
                raise ValueError("LOCAL_HARNESS_ROUTE_DENIED")
            return self.transport.handle_request(request)

        def close(self) -> None:
            self.transport.close()

    previous_client = litellm.client_session
    previous_cache = litellm.in_memory_llm_clients_cache
    with httpx.Client(transport=LocalTransport(), trust_env=False, follow_redirects=False) as client:
        # SDK client cache keys do not include the HTTP client. A fresh cache
        # prevents reuse of an earlier proxy-enabled or closed session.
        litellm.client_session = client
        litellm.in_memory_llm_clients_cache = LLMClientCache()
        try:
            yield
        finally:
            litellm.client_session = previous_client
            litellm.in_memory_llm_clients_cache = previous_cache
