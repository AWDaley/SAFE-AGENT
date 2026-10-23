"""Deterministic evidence retained from correlated, already observed tool results.

This module has no benchmark/catalog/file-system access. It copies native
definitions and literal source spans; it neither summarizes with a model nor
grants discovery, authentication, consent, or execution authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from safe_agent.contracts.episode_inference import wire_digest, wire_json

CONTEXT_RETENTION_PROFILE = "tau3-observed-evidence-v1"
_RETRIEVAL = {"KB_search", "KB_search_bm25", "KB_search_dense", "grep"}
_HEADER = re.compile(
    r"^(?P<rank>[1-9][0-9]*)\. (?P<title>[^\n]{1,512})\n"
    r"   ID: (?P<document>[^\n]{1,256})\n"
    r"   Score: -?[0-9]+\.[0-9]{4}\n   Content: ", re.MULTILINE,
)
_SPAN_CHARACTERS = 768
_OPTIONAL_DOCUMENTS = 8
_STOP_WORDS = {"the", "and", "for", "with", "that", "this", "from", "your", "you", "please"}


class ContextRetentionDenied(ValueError):
    pass


@dataclass(frozen=True)
class ObservedResult:
    call_id: str
    operation: str
    arguments: dict[str, Any]
    result: Any
    success: bool
    history_index: int

    def binding(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id, "operation": self.operation,
            "arguments_hash": wire_digest(self.arguments), "result_hash": wire_digest(self.result),
            "history_index": self.history_index,
        }


@dataclass(frozen=True)
class ObservedDocument:
    source: ObservedResult
    document_id: str | None
    title: str | None
    text: str
    result_start: int
    result_end: int
    rank: int

    @property
    def version(self) -> tuple[str | None, str]:
        return self.document_id, wire_digest(self.text)

    @property
    def key(self) -> tuple[str, str | None, str]:
        return self.source.call_id, *self.version

    def inventory(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "content_hash": self.version[1],
                "source": self.source.binding(), "result_start": self.result_start,
                "result_end": self.result_end}


@dataclass(frozen=True)
class RetainedEvidence:
    required_documents: list[dict[str, Any]]
    optional_documents: list[dict[str, Any]]
    unlocked_definitions: list[dict[str, Any]]
    user_tool_grants: list[dict[str, Any]]
    inventory: dict[str, Any]


def _documents(source: ObservedResult) -> list[ObservedDocument]:
    text = source.result if isinstance(source.result, str) else wire_json(source.result)
    headers = list(_HEADER.finditer(text))
    # A malformed/unrecognized presentation remains an explicitly unstructured
    # observation. Never invent a document ID from arbitrary prose.
    if (not headers or headers[0].start() != 0
            or [int(match["rank"]) for match in headers] != list(range(1, len(headers) + 1))):
        return [ObservedDocument(source, None, None, text, 0, len(text), 1)]
    timing = re.search(r"\n\n\[Timing: retrieval=[^\n]*\]$", text)
    result = []
    for index, header in enumerate(headers):
        if index + 1 < len(headers):
            end = headers[index + 1].start() - 2  # native content newline + record separator
        elif timing:
            end = timing.start() - 1  # native content newline, then timing separator
        else:
            end = len(text) - int(text.endswith("\n"))
        if end < header.end():
            return [ObservedDocument(source, None, None, text, 0, len(text), 1)]
        result.append(ObservedDocument(
            source, header["document"], header["title"], text[header.end():end],
            header.end(), end, int(header["rank"]),
        ))
    return result


def _mentions(text: str, operation: str) -> re.Match[str] | None:
    return re.search(r"(?<!\w)" + re.escape(operation) + r"(?!\w)", text)


def _spans(document: ObservedDocument, operations: set[str]) -> list[dict[str, Any]]:
    text = document.text
    positions = [0]
    # Every active operation mentioned by this source gets a literal window,
    # including definitions occurring far beyond the old prefix-only cutoff.
    for operation in sorted(operations):
        match = _mentions(text, operation)
        if match:
            positions.append(max(0, match.start() - _SPAN_CHARACTERS // 3))
    query = str(document.source.arguments.get("query", document.source.arguments.get("pattern", "")))
    terms = set(re.findall(r"[\w-]{3,}", query.casefold())) - _STOP_WORDS
    if terms and text:
        windows = range(0, len(text), _SPAN_CHARACTERS)
        # Earliest window wins a tie. This is literal relevance, not a semantic
        # claim that the selected excerpt is sufficient to answer the task.
        best = max(windows, key=lambda start: (
            sum(term in text[start:start + _SPAN_CHARACTERS].casefold() for term in terms), -start,
        ))
        positions.append(best)
    ranges: list[tuple[int, int]] = []
    for start in sorted(set(positions)):
        end = min(len(text), start + _SPAN_CHARACTERS)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = ranges[-1][0], max(end, ranges[-1][1])
        else:
            ranges.append((start, end))
    return [{"start": start, "end": end, "text": text[start:end],
             "text_hash": wire_digest(text[start:end])} for start, end in ranges]


def collect_evidence(
    results: list[ObservedResult], *, unlocked_tools: tuple[str, ...],
    discovered_tools: tuple[str, ...], pending_operation: str | None = None,
) -> RetainedEvidence:
    """Pin unlock/discovery sources and the newest retrieval's first document.

    Optional documents are recent distinct versions. All hashes/offsets refer
    to exact native observations before recipient-scoped private projection.
    The complete observations remain in the private journal; the selected
    snapshot is durably recorded in each model request.
    """
    documents = [document for result in results
                 if result.success and result.operation in _RETRIEVAL
                 for document in _documents(result)]
    definitions: dict[str, ObservedResult] = {}
    grants: dict[str, ObservedResult] = {}
    for source in results:
        if not source.success:
            continue
        name = source.arguments.get("agent_tool_name")
        if (source.operation == "unlock_discoverable_agent_tool" and name in unlocked_tools
                and isinstance(source.result, str)
                and source.result.startswith("Tool unlocked: " + str(name) + "\n")):
            definitions[str(name)] = source
        name = source.arguments.get("discoverable_tool_name")
        if (source.operation == "give_discoverable_user_tool" and name in discovered_tools
                and isinstance(source.result, str)
                and source.result.startswith("Tool given to user: " + str(name) + "\n")):
            grants[str(name)] = source
    if set(unlocked_tools) != definitions.keys():
        raise ContextRetentionDenied("EPISODE_UNLOCK_EVIDENCE_MISSING")

    required: dict[tuple[str, str | None, str], set[str]] = {}
    if documents:
        latest = max(document.source.history_index for document in documents)
        current = min((document for document in documents if document.source.history_index == latest),
                      key=lambda document: document.rank)
        required[current.key] = {"latest_retrieval"}
    supporting: dict[str, ObservedDocument] = {}
    for name, source in sorted({**definitions, **grants}.items()):
        candidates = [document for document in documents
                      if document.source.history_index < source.history_index
                      and _mentions(document.text, name)]
        if not candidates:
            raise ContextRetentionDenied("EPISODE_DISCOVERY_EVIDENCE_MISSING")
        chosen = max(candidates, key=lambda document: (document.source.history_index, -document.rank))
        supporting[name] = chosen
        required.setdefault(chosen.key, set()).add(name)
    active = set(unlocked_tools) | set(grants)
    if pending_operation:
        active.add(pending_operation)

    def project(document: ObservedDocument) -> dict[str, Any]:
        return {
            **document.inventory(), "title": document.title,
            "source_format": "pinned_kb_search" if document.document_id is not None else "unstructured",
            "offset_unit": "unicode_codepoints_before_private_projection",
            "required_for": sorted(required.get(document.key, set())),
            "retrieval_query": document.source.arguments.get("query", document.source.arguments.get("pattern")),
            "spans": _spans(document, active),
        }

    pinned = [project(document) for document in documents if document.key in required]
    seen_versions = {document.version for document in documents if document.key in required}
    optional: list[dict[str, Any]] = []
    for document in sorted(documents, key=lambda doc: (-doc.source.history_index, doc.rank)):
        if document.version not in seen_versions:
            seen_versions.add(document.version)
            if len(optional) < _OPTIONAL_DOCUMENTS:
                optional.append(project(document))
    unlocked = [{
        "name": name, "source": source.binding(), "definition_text": source.result,
        "discovery_source": supporting[name].inventory(),
    } for name, source in sorted(definitions.items())]
    given = [{
        "name": name, "actor": "user", "arguments": source.arguments.get("arguments", "{}"),
        "source": source.binding(), "instructions": source.result,
        "discovery_source": supporting[name].inventory(),
        "completion_evidence": "not_implied_by_grant",
    } for name, source in sorted(grants.items())]
    inventory = {
        "observed_document_occurrences": len(documents),
        "observed_document_versions": len({document.version for document in documents}),
        "observed_inventory_hash": wire_digest([document.inventory() for document in documents]),
        "hash_encoding": "sha256_of_canonical_json_value",
        "retained_document_occurrences": len(pinned),
        "omitted_document_occurrences": len(documents) - len(pinned),
        "excerpt_policy": "literal_active_operation_windows_and_query_terms",
    }
    return RetainedEvidence(pinned, optional, unlocked, given, inventory)


def fit_optional(base: dict[str, Any], *, limit: int, field: str, value: Any) -> bool:
    """Append an optional list entry only if the complete prompt still fits."""
    base[field].append(value)
    if len(wire_json(base).encode("utf-8")) > limit:
        base[field].pop()
        return False
    return True
