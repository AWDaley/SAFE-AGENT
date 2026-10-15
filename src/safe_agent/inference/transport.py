"""Provider-neutral, inference-only transport surface.

Only trusted startup code may select an implementation. The model or browser
cannot construct backends, select endpoints, or acquire provider credentials.
Actual external transports, egress and billing will be a separate review gate.
"""

from __future__ import annotations

from typing import Protocol

from safe_agent.contracts.inference import InferenceRequest, InferenceTransportReply


class InferenceTransport(Protocol):
    def infer(self, request: InferenceRequest) -> InferenceTransportReply:
        """Return untrusted bounded text and metadata; never issue an office effect."""
        ...
