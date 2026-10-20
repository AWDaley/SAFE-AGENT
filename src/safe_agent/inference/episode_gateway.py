"""Budgeted, fenced inference for multi-turn benchmark episodes."""

from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    EpisodeCandidate,
    EpisodeDispatchAuthenticator,
    EpisodeDispatchEnvelope,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    parse_candidate,
)
from safe_agent.contracts.episodes import BudgetDelta
from safe_agent.storage.episode_runtime import EpisodeRuntimeStore
from safe_agent.storage.episodes import EpisodeStore


class EpisodeInferenceDenied(RuntimeError):
    """Fixed, sanitized error codes only; never include provider error bodies."""


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

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 60.0):
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def infer(self, envelope: EpisodeDispatchEnvelope) -> EpisodeInferenceReply:
        import os

        import httpx

        try:
            path = self.socket_path
            parent = path.parent.lstat()
            node = path.lstat()
            if (not path.is_absolute() or path.parent.resolve() != path.parent
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077 or not stat.S_ISSOCK(node.st_mode)
                    or node.st_uid != os.getuid() or node.st_mode & 0o077):
                raise ValueError("untrusted socket")
            transport = httpx.HTTPTransport(uds=str(path), retries=0)
            with httpx.Client(transport=transport, timeout=self.timeout_seconds,
                              follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", "http://safe-episode/v1/episode/infer",
                                   content=envelope.model_dump_json(),
                                   headers={"Content-Type": "application/json"}) as response:
                    if response.status_code != 200:
                        raise ValueError("sidecar refused request")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        if len(chunks) + len(chunk) > 131_072:
                            raise ValueError("sidecar reply too large")
                        chunks.extend(chunk)
            return EpisodeInferenceReply.model_validate_json(bytes(chunks))
        except Exception:
            raise EpisodeInferenceDenied("EPISODE_MODEL_TRANSPORT_UNCERTAIN") from None


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
            if (reply.request_id != request.request_id or reply.input_hash != request.digest
                    or reply.model != request.model or self.clock() >= request.deadline
                    or reply.output_tokens > request.max_output_tokens
                    or reply.input_tokens + reply.output_tokens > token_hold):
                raise ValueError("invalid or late inference reply")
            # Persist the exact reply before restoring the episode to READY.
            self.journal.complete_inference(request, reply)
            self.episodes.finish_inference(request.episode_id, request.request_id,
                                           reply.digest, self.clock())
        except Exception:
            try:
                self.journal.uncertain(request.request_id)
                self.episodes.mark_inference_uncertain(
                    request.episode_id, request.request_id,
                    sha256("EPISODE_INFERENCE_OUTCOME_UNCERTAIN"), self.clock(),
                )
            except Exception:
                # A durable DISPATCHING fence still forbids replay if recording
                # uncertainty fails. Do not expose private storage errors.
                pass
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_OUTCOME_UNCERTAIN") from None
        try:
            return parse_candidate(reply.output_json), reply
        except Exception:
            # The provider reply is known and charged. A bounded repair can be
            # a separate durable call; this never retries an unknown outcome.
            raise EpisodeInferenceDenied("EPISODE_INFERENCE_SCHEMA_REJECTED") from None
