"""Network-free mock implementing the provider-neutral inference interface.

This runs a deterministic synthetic fixture, NOT a real language model,
an execution authority, or production isolation.
"""

from __future__ import annotations

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json
from safe_agent.contracts.inference import (
    InferenceReportedUsage,
    InferenceRequest,
    InferenceTransportReply,
)
from safe_agent.model_runtime.offline_worker import respond


class OfflineMockInferenceTransport:
    """Pure deterministic test adapter; has no database, credentials or tools."""

    def infer(self, request: InferenceRequest) -> InferenceTransportReply:
        prompt = canonical_json({
            "instruction": PLANNER_INSTRUCTION,
            "task": request.task,
            "availability": request.availability,
        })
        response = respond(prompt)
        return InferenceTransportReply(
            request_id=request.request_id,
            route_id=request.route_id,
            model_revision=request.model_revision,
            prompt_digest=request.prompt_digest,
            output_json=response.decode("utf-8", errors="strict"),
            reported_usage=InferenceReportedUsage(
                input_tokens=len(prompt), output_tokens=len(response),
                source="synthetic_mock",
            ),
        )
