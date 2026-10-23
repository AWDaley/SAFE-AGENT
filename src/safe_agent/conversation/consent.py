"""Conservative consent for an exact, customer-visible pending action."""

from __future__ import annotations

import re
from typing import Any, Literal

from safe_agent.contracts.episode_inference import wire_digest, wire_json

_YES = {"yes", "yes please", "yes proceed", "yes please proceed", "proceed", "confirmed",
        "i confirm", "confirm", "go ahead", "yes go ahead", "yes i confirm"}
_NO = {"no", "no thanks", "no thank you", "cancel", "do not proceed", "don't proceed"}


def classify_consent(text: str) -> Literal["yes", "no", "ambiguous"]:
    normalized = re.sub(r"[.!?,]+$", "", " ".join(text.casefold().split())).strip()
    if normalized in _YES:
        return "yes"
    if normalized in _NO:
        return "no"
    return "ambiguous"


def confirmation_text(operation: str, arguments: dict[str, Any]) -> str:
    details = "; ".join(f"{name.replace('_', ' ')}: "
                        f"{value if isinstance(value, str) else wire_json(value)}"
                        for name, value in sorted(arguments.items()))
    return (f"Please confirm that you want me to {operation.replace('_', ' ')} "
            f"with these exact details: {details}. Reply yes to approve these details, "
            "or tell me what you want to change.")


def consent_evidence(*, episode_id: str, proposal_digest: str, policy_hash: str,
                     displayed_text: str, user_turn_id: str, user_text: str) -> str:
    if classify_consent(user_text) != "yes":
        raise ValueError("EXPLICIT_CUSTOMER_CONSENT_REQUIRED")
    return wire_digest({"episode_id": episode_id, "proposal_digest": proposal_digest,
                        "policy_hash": policy_hash, "display_hash": wire_digest(displayed_text),
                        "user_turn_id": user_turn_id, "customer_hash": wire_digest(user_text)})
