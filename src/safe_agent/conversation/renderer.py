"""Evidence-bound completion templates and recipient-scoped interpolation.

Free-form policy paraphrases still need semantic evaluation. The conservative
claim filter below is a guardrail, not a proof of every natural-language claim.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from safe_agent.contracts.episode_inference import MessageCandidate
from safe_agent.conversation.intake import ReferenceVault

_COMPLETION = re.compile(
    r"\b(?:successfully|completed|processed|issued|refunded|cancelled|canceled|updated|"
    r"transferred|reversed|approved|has been|have been|i have|i've|all done)\b", re.I)


class ResponseDenied(ValueError):
    pass


def render_response(candidate: MessageCandidate, *, vault: ReferenceVault,
                    subject_id: str | None, authenticated: bool,
                    successful_calls: dict[str, dict[str, Any]], now: datetime) -> str:
    if candidate.stop and candidate.purpose != "finish":
        raise ResponseDenied("STOP_REQUIRES_FINISH_INTENT")
    for call_id in candidate.evidence_call_ids:
        if call_id not in successful_calls:
            raise ResponseDenied("RESPONSE_EVIDENCE_NOT_FOUND")
    if candidate.purpose == "success":
        if not candidate.evidence_call_ids:
            raise ResponseDenied("SUCCESS_REQUIRES_TOOL_RESULT")
        mutations = [successful_calls[call_id] for call_id in candidate.evidence_call_ids
                     if successful_calls[call_id].get("effect") == "business_write"]
        if not mutations:
            raise ResponseDenied("SUCCESS_REQUIRES_MUTATION_RESULT")
        # The planner cannot smuggle an unsupported amount or status into a
        # critical success sentence. Exact details remain in the recorded result.
        operations = ", ".join(str(item["tool_name"]).replace("_", " ") for item in mutations)
        return f"The tool confirmed the requested action: {operations}."
    if _COMPLETION.search(candidate.text):
        raise ResponseDenied("CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE")
    return vault.render(candidate.text, now=now, subject_id=subject_id,
                        authenticated=authenticated)
