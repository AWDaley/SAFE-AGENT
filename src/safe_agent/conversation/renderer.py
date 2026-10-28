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

# Possession ("I have a question") and auxiliaries ("has been") do not by
# themselves claim an operation completed. Keep actual completion vocabulary
# conservative, and permit only the bounded nonassertive forms below.
_COMPLETION = re.compile(
    r"\b(?:successfully|completed|processed|issued|refunded|cancelled|canceled|updated|"
    r"transferred|reversed|approved|all done)\b", re.I)
_ADDITIONAL_COMPLETION = re.compile(
    r"\b(?:done|finished|sent|paid|changed|created|deleted|removed|submitted|closed|opened|"
    r"activated|deactivated|scheduled|booked|reserved|charged|credited|debited|deposited|"
    r"withdrawn|modified|replaced|exchanged|unlocked|enabled|disabled|ordered|purchased)\b", re.I,
)
_ASSERTIVE_AUXILIARY = re.compile(
    r"\b(?:(?:i|we)(?:'ve|\s+(?:have|had))?|is|are|was|were|be|been|has|have|had)\s+"
    r"(?:(?:just|already|now|recently|successfully|correctly|fully)\s+){0,3}$", re.I,
)
_CLAUSE_BOUNDARY = re.compile(r"[.!?;:,\n]+|\b(?:and|but|however|then|therefore)\b", re.I)
_NOUN_WORD = (
    r"(?!(?:i|we|you|it|they|is|are|was|were|has|have|had|been|be|"
    r"will|would|may|might|can|could|should|must|if|whether|that)\b)[a-z][a-z0-9_-]*"
)
_SUBJECT = rf"(?:i|we|you|it|they|(?:my|your|our|the|this|that)\s+(?:{_NOUN_WORD}\s+){{0,5}}{_NOUN_WORD})"
_ADVERBS = r"(?:(?:already|just|recently|successfully|correctly|fully)\s+){0,3}"
_PASSIVE = r"(?:is|are|was|were|(?:has|have|had)\s+been)"
_STATUS_QUESTION = re.compile(
    rf"^(?:(?:is|are|was|were)\s+{_SUBJECT}\s+|"
    rf"(?:has|have|had)\s+{_SUBJECT}\s+(?:been\s+)?){_ADVERBS}$", re.I,
)
_VERIFY_START = (
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|"
    r"(?:i\s+|we\s+)?(?:want|need)\s+to\s+|"
    r"(?:i|we)\s+(?:will|can|would like to)\s+)"
    r"(?:check|verify|confirm|ensure)\s+(?:(?:if|whether|that)\s+)?"
)
_STATUS_CHECK = re.compile(rf"^{_VERIFY_START}{_SUBJECT}\s+{_PASSIVE}\s+{_ADVERBS}$", re.I)
_CONDITION = re.compile(rf"^(?:if|unless)\s+(?:{_SUBJECT}\s+{_PASSIVE}\s+)?{_ADVERBS}$", re.I)
_FUTURE_PASSIVE = re.compile(
    rf"^{_SUBJECT}\s+(?:will|would|may|might|can|could|should|must)\s+"
    rf"(?:not\s+)?be\s+{_ADVERBS}$", re.I,
)
_AFFIRMATIVE_REPLY = re.compile(
    r"^(?:yes(?:\s+indeed)?|indeed|correct|absolutely|certainly|definitely|of course)$", re.I,
)
_ELLIPTICAL_STATUS = re.compile(
    rf"^{_ADVERBS}(?:(?:yes|indeed|absolutely|certainly|definitely)\s+)?"
    r"(?:(?:it|that|this)\s+(?:is|was|has(?:\s+been)?|had(?:\s+been)?|did)|"
    r"they\s+(?:are|were|have(?:\s+been)?|had(?:\s+been)?|did))"
    r"(?:\s+(?:indeed|already|now|true|correct|right|the case))?$", re.I,
)


def _claims_completion(text: str) -> bool:
    """Recognize a bounded set of English completion assertions.

    Every completion occurrence is checked in its own clause. Neither a model
    purpose label, attribution, quoting, nor a question mark exempts a message.
    Unknown phrasing containing completion vocabulary remains denied. This is
    not a semantic proof for arbitrary English or other languages.
    """
    # Normalize only for classification; the original text still passes through
    # the vault. Contractions must not turn an affirmative claim into a question.
    normalized = text.replace("\u2019", "'")
    normalized = re.sub(r"\b(it|that|this)'s\b", r"\1 is", normalized, flags=re.I)
    pending_status: str | None = None
    for clause in _CLAUSE_BOUNDARY.split(normalized):
        clause = clause.strip()
        if not clause:
            continue
        if pending_status is not None:
            # A status question followed by "Yes" or "It was" asserts the
            # effect just as directly as repeating its completion verb. A
            # request to check can be acknowledged ("Yes, I can check"), but
            # cannot turn that acknowledgement into "Yes, it was".
            if _ELLIPTICAL_STATUS.fullmatch(clause):
                return True
            if _AFFIRMATIVE_REPLY.fullmatch(clause):
                if pending_status == "question":
                    return True
                continue
        pending_status = None
        matches = list(_COMPLETION.finditer(clause))
        # Do not restore the broad "I have" false positive, or reject every
        # occurrence of "changed" in a customer's reported prior change. These
        # additional action verbs require an agent/perfect/passive construction.
        matches.extend(match for match in _ADDITIONAL_COMPLETION.finditer(clause)
                       if _ASSERTIVE_AUXILIARY.search(clause[:match.start()]))
        for match in matches:
            prefix = clause[:match.start()]
            status = ("question" if _STATUS_QUESTION.fullmatch(prefix)
                      else "check" if _STATUS_CHECK.fullmatch(prefix) else None)
            if status is not None:
                suffix = clause[match.end():].strip()
                if (_ELLIPTICAL_STATUS.fullmatch(suffix)
                        or (status == "question" and _AFFIRMATIVE_REPLY.fullmatch(suffix))):
                    return True
                pending_status = status
                continue
            if _CONDITION.fullmatch(prefix) or _FUTURE_PASSIVE.fullmatch(prefix):
                continue
            return True
    return False


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
    if _claims_completion(candidate.text):
        raise ResponseDenied("CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE")
    return vault.render(candidate.text, now=now, subject_id=subject_id,
                        authenticated=authenticated)
