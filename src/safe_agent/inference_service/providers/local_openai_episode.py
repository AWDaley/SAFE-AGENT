"""Explicit loopback Chat Completions route; no hosted credentials or fallback.

The route establishes a numeric loopback destination, not the model server's
weights, hardware, cloud configuration, or outbound network policy.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from safe_agent.contracts.episode_inference import (
    CANDIDATE_ADAPTER,
    EpisodeBackendCapabilities,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    strict_json,
    validate_local_api_base,
    wire_digest,
    wire_json,
)
from safe_agent.inference_service.providers.openai_episode import episode_system_instructions

MAX_PROVIDER_RESPONSE_BYTES = 262_144


def local_openai_backend_capabilities(
    *, model: str, base_url: str, timeout_seconds: float = 60.0,
) -> EpisodeBackendCapabilities:
    """Pure expected configuration; does not query or load a local model."""
    return EpisodeBackendCapabilities(
        profile="local_openai_chat_v1", model=model,
        provider_origin=validate_local_api_base(base_url), timeout_seconds=timeout_seconds,
        response_format="json_object",
        candidate_schema_hash=wire_digest(CANDIDATE_ADAPTER.json_schema()),
        system_instruction_hash=wire_digest(episode_system_instructions()),
    )


class LocalOpenAIEpisodeBackend:
    """One bounded, nonstreaming JSON response per authorized model attempt."""

    def __init__(self, *, model: str, base_url: str, timeout_seconds: float = 60.0):
        self._capabilities = local_openai_backend_capabilities(
            model=model, base_url=base_url, timeout_seconds=timeout_seconds,
        )
        self._instructions = episode_system_instructions()
        self._url = base_url + "/chat/completions"
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(retries=0, trust_env=False),
            timeout=self.capabilities.timeout_seconds, follow_redirects=False, trust_env=False,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Accept-Encoding": "identity"},
        )

    @property
    def capabilities(self) -> EpisodeBackendCapabilities:
        return self._capabilities

    @property
    def model(self) -> str:
        return self._capabilities.model

    def close(self) -> None:
        self._client.close()

    def infer(self, request: EpisodeInferenceRequest) -> EpisodeInferenceReply:
        if request.model != self.model:
            raise ValueError("MODEL_ROUTE_DENIED")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self._instructions},
                         {"role": "user", "content": wire_json(request.projected_context)}],
            # Ollama's compatibility API documents max_tokens. The hosted
            # backend retains its existing max_completion_tokens behavior.
            "max_tokens": request.max_output_tokens, "temperature": request.temperature,
            "response_format": {"type": "json_object"}, "stream": False,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        deadline = time.monotonic() + self.capabilities.timeout_seconds
        try:
            with self._client.stream("POST", self._url, content=wire_json(payload)) as response:
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ValueError("invalid local response status or format")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if (time.monotonic() >= deadline
                            or len(data) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES):
                        raise ValueError("local response limit exceeded")
                    data.extend(chunk)
                if time.monotonic() >= deadline:
                    raise ValueError("local response deadline exceeded")
            parsed = strict_json(data.decode("utf-8"), max_bytes=MAX_PROVIDER_RESPONSE_BYTES)
            return self._reply(parsed, request)
        except Exception:
            # Never expose model text, raw HTTP errors, or local filesystem
            # details. An uncertain attempt is not retried or sent elsewhere.
            raise ValueError("LOCAL_MODEL_RESPONSE_UNCERTAIN") from None

    def _reply(self, parsed: object, request: EpisodeInferenceRequest) -> EpisodeInferenceReply:
        if not isinstance(parsed, dict) or parsed.get("model") != self.model:
            raise ValueError("invalid local response model")
        choices = parsed.get("choices")
        usage = parsed.get("usage")
        if (not isinstance(choices, list) or len(choices) != 1
                or not isinstance(choices[0], dict) or choices[0].get("finish_reason") != "stop"
                or not isinstance(usage, dict)):
            raise ValueError("incomplete local response")
        message = choices[0].get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("tool_calls") or message.get("refusal")):
            raise ValueError("invalid local response message")
        output = message.get("content")
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if (not isinstance(output, str) or not 0 < len(output.encode("utf-8")) <= 65_536
                or type(input_tokens) is not int or input_tokens < 0
                or type(output_tokens) is not int or not 0 <= output_tokens <= request.max_output_tokens):
            raise ValueError("invalid local response content or token usage")
        total_tokens = usage.get("total_tokens")
        if (total_tokens is not None
                and (type(total_tokens) is not int or total_tokens != input_tokens + output_tokens)):
            raise ValueError("invalid local total token usage")
        return EpisodeInferenceReply(
            request_id=request.request_id, input_hash=request.digest, model=request.model,
            output_json=output, input_tokens=input_tokens, output_tokens=output_tokens,
            usage_source="provider_reported",
        )
