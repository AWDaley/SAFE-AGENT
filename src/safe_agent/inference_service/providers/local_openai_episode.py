"""Explicit loopback Chat Completions route; no hosted credentials or fallback.

The route establishes a numeric loopback destination, not the model server's
weights, hardware, cloud configuration, or outbound network policy.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from safe_agent.contracts.episode_inference import (
    CANDIDATE_ADAPTER,
    EpisodeBackendCapabilities,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    LocalTimingProfile,
    strict_json,
    validate_local_api_base,
    wire_digest,
    wire_json,
)
from safe_agent.inference.deadline_http import DeadlineBudget, DeadlineHTTPTransport
from safe_agent.inference_service.providers.openai_episode import episode_system_instructions

MAX_PROVIDER_RESPONSE_BYTES = 262_144


def local_openai_backend_capabilities(
    *, model: str, base_url: str, timeout_seconds: float | None = None,
    local_timing: LocalTimingProfile | None = None,
) -> EpisodeBackendCapabilities:
    """Pure expected configuration; does not query or load a local model."""
    if (local_timing is not None and timeout_seconds is not None
            and timeout_seconds != local_timing.provider_timeout_seconds):
        raise ValueError("LOCAL_TIMING_CAPABILITY_MISMATCH")
    timeout = (local_timing.provider_timeout_seconds if local_timing is not None
               else 60.0 if timeout_seconds is None else timeout_seconds)
    return EpisodeBackendCapabilities(
        profile="local_openai_chat_v1", model=model,
        provider_origin=validate_local_api_base(base_url), timeout_seconds=timeout,
        response_format="json_object",
        candidate_schema_hash=wire_digest(CANDIDATE_ADAPTER.json_schema()),
        system_instruction_hash=wire_digest(episode_system_instructions()),
        local_timing=local_timing,
    )


class LocalModelFailure(ValueError):
    """Preserve the public uncertainty contract with a fixed diagnostic label."""

    def __init__(self, reason_code: str):
        super().__init__("LOCAL_MODEL_RESPONSE_UNCERTAIN")
        self.reason_code = reason_code


class LocalOpenAIEpisodeBackend:
    """One bounded, nonstreaming JSON response per authorized model attempt."""

    def __init__(self, *, model: str, base_url: str, timeout_seconds: float | None = None,
                 local_timing: LocalTimingProfile | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 monotonic: Callable[[], float] = time.monotonic):
        self._capabilities = local_openai_backend_capabilities(
            model=model, base_url=base_url, timeout_seconds=timeout_seconds, local_timing=local_timing,
        )
        self._instructions = episode_system_instructions()
        self._url = base_url + "/chat/completions"
        self.clock = clock
        self.monotonic = monotonic
        self._active: dict[str, DeadlineBudget] = {}
        self._lock = threading.Lock()
        # Historical conditions keep their original connection/timeout setup.
        # Explicit profiles get one independently cancellable connection per call.
        self._client = None if local_timing is not None else httpx.Client(
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
        with self._lock:
            budgets = list(self._active.values())
        for budget in budgets:
            budget.cancel()
        if self._client is not None:
            self._client.close()

    def cancel(self, request_id: str) -> None:
        with self._lock:
            budget = self._active.get(request_id)
        if budget is not None:
            budget.cancel()

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
        profile = self.capabilities.local_timing
        allowance = self.capabilities.timeout_seconds
        if profile is not None:
            now = self.clock()
            provider_end = request.issued_at + timedelta(seconds=profile.provider_timeout_seconds)
            allowance = (provider_end - now).total_seconds()
            if (not request.issued_at <= now < request.deadline or allowance <= 0
                    or (request.deadline - request.issued_at).total_seconds()
                    != profile.request_timeout_seconds):
                raise LocalModelFailure("EPISODE_INFERENCE_LIFETIME_INSUFFICIENT")
        deadline = self.monotonic() + allowance
        budget = DeadlineBudget(deadline, clock=self.monotonic) if profile is not None else None
        client = self._client
        if budget is not None:
            with self._lock:
                if request.request_id in self._active:
                    raise LocalModelFailure("LOCAL_MODEL_RESPONSE_INVALID")
                self._active[request.request_id] = budget
            try:
                client = httpx.Client(
                    transport=DeadlineHTTPTransport(budget), timeout=allowance,
                    follow_redirects=False, trust_env=False,
                    headers={"Content-Type": "application/json", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                )
            except Exception:
                with self._lock:
                    self._active.pop(request.request_id, None)
                raise LocalModelFailure("LOCAL_MODEL_RESPONSE_INVALID") from None
        assert client is not None
        try:
            with client.stream("POST", self._url, content=wire_json(payload)) as response:
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ValueError("invalid local response status or format")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if self.monotonic() >= deadline:
                        raise LocalModelFailure("LOCAL_MODEL_RESPONSE_DEADLINE_EXCEEDED")
                    if len(data) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise LocalModelFailure("LOCAL_MODEL_RESPONSE_LIMIT_EXCEEDED")
                    data.extend(chunk)
                if self.monotonic() >= deadline:
                    raise LocalModelFailure("LOCAL_MODEL_RESPONSE_DEADLINE_EXCEEDED")
            parsed = strict_json(data.decode("utf-8"), max_bytes=MAX_PROVIDER_RESPONSE_BYTES)
            reply = self._reply(parsed, request)
            if self.monotonic() >= deadline:
                raise LocalModelFailure("LOCAL_MODEL_RESPONSE_DEADLINE_EXCEEDED")
            return reply
        except LocalModelFailure:
            raise
        except httpx.TimeoutException:
            raise LocalModelFailure("LOCAL_MODEL_RESPONSE_DEADLINE_EXCEEDED") from None
        except httpx.ConnectError:
            raise LocalModelFailure("LOCAL_MODEL_CONNECT_FAILED") from None
        except Exception:
            # Never expose model text, raw HTTP errors, or local filesystem
            # details. An uncertain attempt is not retried or sent elsewhere.
            reason = ("LOCAL_MODEL_REQUEST_CANCELLED" if budget is not None and budget.cancelled
                      else "LOCAL_MODEL_RESPONSE_INVALID")
            raise LocalModelFailure(reason) from None
        finally:
            if budget is not None:
                client.close()
                with self._lock:
                    self._active.pop(request.request_id, None)

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
