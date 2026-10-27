"""Budgeted, fenced inference for multi-turn benchmark episodes."""

from __future__ import annotations

import math
import stat
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Protocol

from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    INFERENCE_FAILURE_REASONS,
    EpisodeCandidate,
    EpisodeCapabilitiesReplyEnvelope,
    EpisodeCapabilitiesRequest,
    EpisodeDispatchAuthenticator,
    EpisodeDispatchEnvelope,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    EpisodeServiceCapabilities,
    LocalTimingProfile,
    inference_failure_reason,
    parse_candidate,
    strict_json,
)
from safe_agent.contracts.episodes import BudgetDelta
from safe_agent.storage.episode_runtime import EpisodeRuntimeStore
from safe_agent.storage.episodes import EpisodeStore


class EpisodeInferenceDenied(RuntimeError):
    """Fixed, sanitized error codes only; never include provider error bodies."""

    def __init__(self, message: str, *, reason_code: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code if reason_code in INFERENCE_FAILURE_REASONS else (
            "EPISODE_INFERENCE_OUTCOME_UNCERTAIN"
        )


class EpisodeTransport(Protocol):
    def infer(self, envelope: EpisodeDispatchEnvelope) -> EpisodeInferenceReply: ...


class ScriptedEpisodeTransport:
    """Explicit test backend. Fixtures are trusted host code, not planner plugins."""

    def __init__(self, outputs: Sequence[str | EpisodeCandidate] | Callable[[EpisodeInferenceRequest], str]):
        self._outputs = outputs
        self.requests: list[EpisodeInferenceRequest] = []

    def infer(self, envelope: EpisodeDispatchEnvelope) -> EpisodeInferenceReply:
        request = envelope.request
        index = len(self.requests)
        self.requests.append(request)
        if callable(self._outputs):
            output = self._outputs(request)
        else:
            item = self._outputs[index]
            output = item if isinstance(item, str) else item.model_dump_json()
        return EpisodeInferenceReply(request_id=request.request_id, input_hash=request.digest,
                                     model=request.model, output_json=output,
                                     input_tokens=0, output_tokens=0, usage_source="scripted")


class EpisodeUnixTransport:
    """Fixed local UDS endpoint; no model-supplied URL, redirect or HTTP retry."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float | None = None,
                 local_timing: LocalTimingProfile | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 monotonic: Callable[[], float] = time.monotonic):
        self.socket_path = socket_path
        if local_timing is not None and timeout_seconds is not None and (
            timeout_seconds != local_timing.transport_timeout_seconds
        ):
            raise ValueError("LOCAL_TIMING_TRANSPORT_MISMATCH")
        self.local_timing = local_timing
        self.timeout_seconds = (local_timing.transport_timeout_seconds if local_timing is not None
                                else 60.0 if timeout_seconds is None else timeout_seconds)
        self.clock = clock
        self.monotonic = monotonic

    def _post(self, route: str, body: str, *, max_reply_bytes: int,
              deadline: float | None = None) -> bytes:
        import os

        import httpx

        path = self.socket_path
        parent = path.parent.lstat()
        node = path.lstat()
        if (not path.is_absolute() or path.parent.resolve() != path.parent
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077 or not stat.S_ISSOCK(node.st_mode)
                or node.st_uid != os.getuid() or node.st_mode & 0o077):
            raise ValueError("untrusted socket")
        transport: httpx.BaseTransport
        if deadline is None:
            transport = httpx.HTTPTransport(uds=str(path), retries=0)
        else:
            from safe_agent.inference.deadline_http import DeadlineBudget, DeadlineHTTPTransport

            transport = DeadlineHTTPTransport(DeadlineBudget(deadline, clock=self.monotonic), uds=str(path))
        with httpx.Client(transport=transport, timeout=self.timeout_seconds,
                          follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", "http://safe-episode" + route,
                               content=body, headers={"Content-Type": "application/json"}) as response:
                if response.status_code != 200:
                    raise EpisodeInferenceDenied(
                        "EPISODE_MODEL_TRANSPORT_UNCERTAIN",
                        reason_code=response.headers.get("x-safe-inference-reason"),
                    )
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunks) + len(chunk) > max_reply_bytes:
                        raise ValueError("sidecar reply too large")
                    chunks.extend(chunk)
        return bytes(chunks)

    def infer(self, envelope: EpisodeDispatchEnvelope) -> EpisodeInferenceReply:
        import httpx

        try:
            if self.local_timing is None:
                raw = self._post("/v1/episode/infer", envelope.model_dump_json(), max_reply_bytes=131_072)
            else:
                request = envelope.request
                now = self.clock()
                expected = self.local_timing.request_timeout_seconds
                remaining = (request.deadline - now).total_seconds()
                if ((request.deadline - request.issued_at).total_seconds() != expected
                        or remaining <= 0):
                    raise EpisodeInferenceDenied("EPISODE_MODEL_TRANSPORT_UNCERTAIN",
                        reason_code="EPISODE_INFERENCE_LIFETIME_INSUFFICIENT")
                # Admission is the shared origin, rather than a fresh allowance
                # after host persistence or time spent waiting for a socket.
                transport_end = request.issued_at + timedelta(seconds=self.timeout_seconds)
                allowance = min(remaining, (transport_end - now).total_seconds())
                if allowance <= 0:
                    raise EpisodeInferenceDenied("EPISODE_MODEL_TRANSPORT_UNCERTAIN",
                        reason_code="EPISODE_INFERENCE_LIFETIME_INSUFFICIENT")
                raw = self._post("/v1/episode/infer", envelope.model_dump_json(), max_reply_bytes=131_072,
                                 deadline=self.monotonic() + allowance)
            return EpisodeInferenceReply.model_validate_json(raw)
        except EpisodeInferenceDenied:
            raise
        except httpx.TimeoutException:
            raise EpisodeInferenceDenied("EPISODE_MODEL_TRANSPORT_UNCERTAIN",
                reason_code="EPISODE_MODEL_TRANSPORT_TIMEOUT") from None
        except Exception:
            raise EpisodeInferenceDenied("EPISODE_MODEL_TRANSPORT_UNCERTAIN",
                reason_code="EPISODE_MODEL_TRANSPORT_UNAVAILABLE") from None

    def capabilities(self, authenticator: EpisodeDispatchAuthenticator, *,
                     expected: EpisodeServiceCapabilities,
                     clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                     absolute_timeout_seconds: float | None = None) -> EpisodeCapabilitiesReplyEnvelope:
        """Fresh signed readiness; no budget, journal, or provider operation."""
        try:
            if absolute_timeout_seconds is not None and (
                type(absolute_timeout_seconds) not in (int, float)
                or not math.isfinite(absolute_timeout_seconds)
                or not 0 < absolute_timeout_seconds <= 10
            ):
                raise ValueError("invalid capability deadline")
            now = clock()
            challenge = EpisodeCapabilitiesRequest(
                nonce=token_hex(32), model=expected.backend.model,
                issued_at=now, deadline=now + timedelta(seconds=10),
            )
            envelope = authenticator.sign_capabilities_request(challenge)
            if absolute_timeout_seconds is None:
                raw = self._post("/v1/episode/capabilities", envelope.model_dump_json(), max_reply_bytes=16_384)
            else:
                # Local preflight opts in to an absolute body deadline. Hosted
                # and existing callers retain the original transport behavior.
                allowance = min(self.timeout_seconds, absolute_timeout_seconds,
                                (challenge.deadline - now).total_seconds())
                raw = self._post("/v1/episode/capabilities", envelope.model_dump_json(),
                                 max_reply_bytes=16_384, deadline=self.monotonic() + allowance)
            strict_json(raw.decode("utf-8"), max_bytes=16_384)
            signed = EpisodeCapabilitiesReplyEnvelope.model_validate_json(raw)
            reply = authenticator.verify_capabilities_reply(signed)
            if (reply.nonce != challenge.nonce or reply.request_hash != challenge.digest
                    or reply.deadline != challenge.deadline
                    or not challenge.issued_at <= reply.issued_at <= clock() < reply.deadline
                    or reply.capabilities != expected):
                raise ValueError("stale or mismatched sidecar capabilities")
            return signed
        except Exception:
            raise EpisodeInferenceDenied("EPISODE_CAPABILITIES_VERIFICATION_FAILED") from None


class EpisodeInferenceGateway:
    def __init__(self, *, episodes: EpisodeStore, journal: EpisodeRuntimeStore,
                 transport: EpisodeTransport, authenticator: EpisodeDispatchAuthenticator,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        if type(transport) not in {ScriptedEpisodeTransport, EpisodeUnixTransport}:
            raise TypeError("an explicit scripted or isolated episode model transport is required")
        self.episodes = episodes
        self.journal = journal
        self.transport = transport
        self.authenticator = authenticator
        self.clock = clock

    def infer(self, request: EpisodeInferenceRequest, *, token_hold: int,
              cost_hold_microusd: int) -> tuple[EpisodeCandidate, EpisodeInferenceReply]:
        now = self.clock()
        if not request.issued_at <= now < request.deadline:
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_DEADLINE_EXCEEDED")
        try:
            snapshot = self.episodes.inspect(request.episode_id)
            if (request.domain != snapshot.domain or request.policy_hash != snapshot.policy_hash
                    or request.observation_hash != snapshot.observation_hash
                    or request.deadline > snapshot.deadline
                    or (request.deadline - request.issued_at).total_seconds() > 120
                    or token_hold < request.max_output_tokens):
                raise ValueError("episode request source or allowance mismatch")
            reservation = self.episodes.reserve_inference(
                request.episode_id, request.request_id, request.digest,
                BudgetDelta(model_calls=1, tokens=token_hold, cost_microusd=cost_hold_microusd), now,
                expected_version=snapshot.state_version,
            )
            self.journal.prepare_inference(request)
            self.journal.claim_inference(request)
            envelope = self.authenticator.sign(request, sha256(reservation))
        except Exception:
            # Preparation failures may contain private DB parameters. A prior
            # dispatch is never retried, nor is its durable allowance refunded.
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_PREPARATION_DENIED") from None
        try:
            reply = self.transport.infer(envelope)
            if self.clock() >= request.deadline:
                raise EpisodeInferenceDenied("EPISODE_INFERENCE_OUTCOME_UNCERTAIN",
                    reason_code="EPISODE_INFERENCE_REPLY_LATE")
            if (reply.request_id != request.request_id or reply.input_hash != request.digest
                    or reply.model != request.model
                    or reply.output_tokens > request.max_output_tokens
                    or reply.input_tokens + reply.output_tokens > token_hold):
                raise EpisodeInferenceDenied("EPISODE_INFERENCE_OUTCOME_UNCERTAIN",
                    reason_code="EPISODE_MODEL_REPLY_INVALID")
            # Persist the exact reply before restoring the episode to READY.
            self.journal.complete_inference(request, reply)
            self.episodes.finish_inference(request.episode_id, request.request_id,
                                           reply.digest, self.clock())
        except Exception as exc:
            reason = inference_failure_reason(exc)
            try:
                self.journal.uncertain(request.request_id)
                self.episodes.mark_inference_uncertain(
                    request.episode_id, request.request_id,
                    sha256(reason), self.clock(),
                )
            except Exception:
                # A durable DISPATCHING fence still forbids replay if recording
                # uncertainty fails. Do not expose private storage errors.
                pass
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_OUTCOME_UNCERTAIN", reason_code=reason) from None
        try:
            return parse_candidate(reply.output_json), reply
        except Exception:
            # The provider reply is known and charged. A bounded repair can be
            # a separate durable call; this never retries an unknown outcome.
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_SCHEMA_REJECTED") from None
