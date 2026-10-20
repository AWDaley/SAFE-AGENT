"""Field-aware projections for the synthetic text benchmark.

Known identity fields, resource identifiers, and recognized email/phone/SSN
patterns are protected. This is not a universal PII detector: unclassified
synthetic free text remains visible and this limitation is recorded in the run
profile. A bounded fallback may ground a new private scalar in an exact token
from a host-supplied customer message. This does not prove identity, consent, or
universal privacy. Production customer text needs a separate reviewed intake policy.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from safe_agent.contracts.episode_inference import strict_json, wire_digest

_REFERENCE = re.compile(r"\[\[safe:[A-Za-z0-9_]+:[a-f0-9]{32}\]\]")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-]|\.[\w-])")
_PHONE = re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_BANK_HEADER = re.compile(r"^Found \d+ record\(s\) in '([A-Za-z_]+)':$")
_BANK_RECORD = re.compile(r"^(\d+\. Record ID: )(.+)$")
_BANK_FIELD = re.compile(r"^(   )([A-Za-z_]+)(: )(.*)$")
_PUBLIC_RESOURCE_KINDS = {"product_id", "item_id"}

_ALIASES = {
    "email_address": "email", "phone_number": "phone", "mobile_number": "phone",
    "date_of_birth": "dob", "birth_date": "dob", "birthdate": "dob",
    "zip_code": "zip", "zipcode": "zip", "postal_code": "zip",
    "address1": "address", "address2": "address", "street": "address",
    "account_number": "account_id", "card_number": "card_id", "ssn": "ssn",
    "orders": "order_ids", "customer_name": "person_name", "full_name": "person_name",
}
_IDENTITY = {"email", "phone", "dob", "first_name", "last_name", "person_name", "zip", "ssn"}


def private_kind(key: str) -> str | None:
    key = _ALIASES.get(key.lower(), key.lower())
    if key in _IDENTITY or key == "address":
        return key
    if key.endswith("_ids"):
        return key[:-1]
    if key.endswith("_id") and key not in {"document_id", "tool_call_id", "call_id"}:
        return key
    return None


class ProjectionDenied(ValueError):
    pass


class ReferenceVault:
    """Host-owned data structure persisted only in private episode context."""

    def __init__(self, *, episode_id: str, actor_id: str, deadline: datetime,
                 tools: list[dict[str, Any]], records: list[dict[str, Any]] | None = None,
                 customer_texts: tuple[str, ...] = (),
                 resource_subjects: dict[str, str] | None = None):
        if (not isinstance(customer_texts, tuple) or len(customer_texts) > 1000
                or any(not isinstance(value, str) or len(value.encode("utf-8")) > 65_536
                       for value in customer_texts)):
            raise ProjectionDenied("CUSTOMER_SOURCE_TEXT_BOUND_EXCEEDED")
        self.episode_id = episode_id
        self.actor_id = actor_id
        self.deadline = deadline
        self.records = records if records is not None else []
        self._tool_names = {str(tool["name"]) for tool in tools}
        # This tuple must come from authenticated host intake, never model or
        # tool content. Storing it here does not send it to a model.
        self._customer_texts = customer_texts
        self._resource_subjects = dict(resource_subjects or {})

    def reference(self, value: Any, *, kind: str, source: str,
                  subject_id: str | None, operations: set[str] | None = None) -> str:
        allowed = operations if operations is not None else self._tool_names
        if not allowed <= self._tool_names:
            raise ProjectionDenied("PRIVATE_REFERENCE_OPERATION_NOT_REGISTERED")
        if isinstance(value, str) and _REFERENCE.fullmatch(value):
            matches = [record for record in self.records if record["reference"] == value]
            if (len(matches) != 1 or matches[0]["kind"] != kind
                    or matches[0]["episode_id"] != self.episode_id
                    or matches[0]["actor_id"] != self.actor_id or matches[0]["revoked"]):
                raise ProjectionDenied("PRIVATE_REFERENCE_SCOPE_DENIED")
            # Reprojection never wraps a handle in another handle or rewrites its
            # original source/operation/subject scope. Opaque foreign handles
            # can remain visible; resolution still rejects the foreign subject.
            return value
        for record in self.records:
            if (wire_digest(record["value"]) == wire_digest(value) and record["kind"] == kind
                    and record["source"] == source and record["subject_id"] == subject_id
                    and record["episode_id"] == self.episode_id
                    and record["actor_id"] == self.actor_id
                    and set(record["operations"]) == allowed
                    and record["expires_at"] == self.deadline.isoformat()
                    and not record["revoked"]):
                return str(record["reference"])
        token = f"[[safe:{kind}:{uuid4().hex}]]"
        self.records.append({
            "reference": token, "value": deepcopy(value), "kind": kind,
            "episode_id": self.episode_id, "actor_id": self.actor_id,
            "source": source, "subject_id": subject_id, "revoked": False,
            "operations": sorted(allowed),
            "expires_at": self.deadline.isoformat(),
        })
        return token

    def _record(self, token: str, *, now: datetime, subject_id: str | None) -> dict[str, Any]:
        records = [record for record in self.records if record["reference"] == token]
        if len(records) != 1:
            raise ProjectionDenied("UNKNOWN_PRIVATE_REFERENCE")
        record = records[0]
        if (record["episode_id"] != self.episode_id or record["actor_id"] != self.actor_id
                or record["revoked"] or now >= datetime.fromisoformat(record["expires_at"])
                or (record["subject_id"] is not None and record["subject_id"] != subject_id)
                or (record["source"] != "customer" and record["subject_id"] is None
                    and record["kind"] not in _PUBLIC_RESOURCE_KINDS)):
            raise ProjectionDenied("PRIVATE_REFERENCE_SCOPE_DENIED")
        return record

    def project_text(self, value: str, *, source: str, subject_id: str | None) -> str:
        # Prefer the longest known value so an address does not partially leak.
        known = sorted((record for record in self.records if isinstance(record["value"], str)
                        and len(record["value"]) >= 3),
                       key=lambda r: (len(r["value"]), r["subject_id"] == subject_id), reverse=True)
        result = value
        for record in known:
            def replace_known(text: str, current: dict[str, Any] = record) -> str:
                token = current["reference"]
                if source == "customer" and current["value"] in text:
                    token = self.reference(current["value"], kind=current["kind"],
                                           source="customer", subject_id=subject_id)
                return text.replace(current["value"], token)
            result = self._outside_references(result, replace_known)
        for kind, pattern in (("ssn", _SSN), ("email", _EMAIL), ("phone", _PHONE)):
            def replace_identity(text: str, current: re.Pattern[str] = pattern,
                                 current_kind: str = kind) -> str:
                return current.sub(lambda match: self.reference(
                    match.group(), kind=current_kind, source=source, subject_id=subject_id), text)
            result = self._outside_references(result, replace_identity)
        return result

    @staticmethod
    def _outside_references(text: str, transform: Callable[[str], str]) -> str:
        chunks: list[str] = []
        position = 0
        for match in _REFERENCE.finditer(text):
            chunks.extend((transform(text[position:match.start()]), match.group()))
            position = match.end()
        chunks.append(transform(text[position:]))
        return "".join(chunks)

    def _seed_fields(self, value: Any, *, source: str,
                     subject_id: str | None, key: str = "") -> None:
        subject_id = self._record_subject(value, source=source, subject_id=subject_id)
        kind = private_kind(key)
        if kind and value not in (None, ""):
            for item in value if isinstance(value, list) else [value]:
                self.reference(item, kind=kind, source=source, subject_id=subject_id)
        if isinstance(value, dict):
            for name, child in value.items():
                binding = self._private_key(name, child, parent_key=key)
                child_subject = self._key_subject(name, child, source=source, subject_id=subject_id)
                if binding is not None:
                    kind, native = binding
                    self.reference(native, kind=kind, source=source, subject_id=child_subject)
                self._seed_fields(child, key=self._child_key(key, name, value), source=source,
                                  subject_id=child_subject)
        elif isinstance(value, list):
            for child in value:
                self._seed_fields(child, key=key, source=source, subject_id=subject_id)

    @staticmethod
    def _child_key(parent: str, name: str, record: dict[str, Any] | None = None) -> str:
        if parent == "name":
            return {"first": "first_name", "last": "last_name"}.get(name, name)
        if name == "name" and record and isinstance(record.get("name"), str) and "user_id" in record:
            return "person_name"
        return name

    @staticmethod
    def _record_subject(value: Any, *, source: str, subject_id: str | None) -> str | None:
        if source != "customer" and isinstance(value, dict):
            owner = value.get("user_id")
            if isinstance(owner, str) and owner and not _REFERENCE.fullmatch(owner):
                return owner
        return subject_id

    def _key_subject(self, name: str, child: Any, *, source: str,
                     subject_id: str | None) -> str | None:
        if source != "customer":
            if name.startswith("user:"):
                subject_id = name.removeprefix("user:")
            elif name in self._resource_subjects:
                subject_id = self._resource_subjects[name]
        return self._record_subject(child, source=source, subject_id=subject_id)

    def _banking_text(self, value: str) -> str | None:
        """Mask the pinned database presentation without assigning query-wide ownership."""
        lines = value.splitlines(keepends=True)
        if not any(_BANK_HEADER.fullmatch(line.rstrip("\r\n")) for line in lines):
            return None
        table: str | None = None
        output: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            bare = line.rstrip("\r\n")
            header = _BANK_HEADER.fullmatch(bare)
            if header:
                table = header.group(1)
                output.append(line)
                index += 1
                continue
            record = _BANK_RECORD.fullmatch(bare)
            if not record or table is None:
                output.append(self.project_text(line, source="tool", subject_id=None))
                index += 1
                continue
            end = index + 1
            while end < len(lines) and not (
                _BANK_RECORD.fullmatch(lines[end].rstrip("\r\n"))
                or _BANK_HEADER.fullmatch(lines[end].rstrip("\r\n"))
            ):
                end += 1
            fields: dict[str, list[str]] = {}
            for field_line in lines[index + 1:end]:
                field = _BANK_FIELD.fullmatch(field_line.rstrip("\r\n"))
                if field:
                    fields.setdefault(field.group(2), []).append(field.group(4))
            native = record.group(2)
            owners = fields.get("user_id", [])
            subject = owners[0] if len(owners) == 1 else None
            if table == "users":
                subject = native if not owners or owners == [native] else None
            elif subject is None:
                subject = self._resource_subjects.get("bank:" + native)
            record_kind = "user_id" if table == "users" else "resource_id"
            for name, values in fields.items():
                if values == [native] and private_kind(name):
                    record_kind = private_kind(name) or record_kind
            record_token = self.reference(native, kind=record_kind, source="tool", subject_id=subject)
            output.append(record.group(1) + record_token + line[len(bare):])
            for field_line in lines[index + 1:end]:
                field_bare = field_line.rstrip("\r\n")
                field = _BANK_FIELD.fullmatch(field_bare)
                if field:
                    name = "person_name" if table == "users" and field.group(2) == "name" else field.group(2)
                    projected = self.project(field.group(4), key=name, source="tool", subject_id=subject)
                    output.append(field.group(1) + field.group(2) + field.group(3)
                                  + str(projected) + field_line[len(field_bare):])
                else:
                    output.append(self.project_text(field_line, source="tool", subject_id=subject))
            index = end
        return "".join(output)

    @staticmethod
    def _private_key(name: str, child: Any, *, parent_key: str) -> tuple[str, str] | None:
        if parent_key in {"payment_methods", "variants"}:
            return ("payment_method_id" if parent_key == "payment_methods" else "item_id", name)
        prefix, separator, native = name.partition(":")
        if separator and native and prefix in {"user", "order", "product", "item"}:
            return prefix + "_id", native
        if separator and native and prefix == "bank" and native != "current_time":
            if isinstance(child, dict):
                for field, value in child.items():
                    kind = private_kind(field)
                    if kind is not None and value == native:
                        return kind, native
            # Unknown bank record keys remain opaque without being promoted to
            # an account/card capability. Known child fields retain their kinds.
            return "resource_id", native
        return None

    def project(self, value: Any, *, source: str, subject_id: str | None,
                key: str = "") -> Any:
        # Register structured private fields before projecting any sibling free
        # text, so field order cannot expose a known identifier or identity value.
        self._seed_fields(value, key=key, source=source, subject_id=subject_id)
        return self._project(value, key=key, source=source, subject_id=subject_id)

    def _project(self, value: Any, *, source: str, subject_id: str | None,
                 key: str = "") -> Any:
        subject_id = self._record_subject(value, source=source, subject_id=subject_id)
        kind = private_kind(key)
        if kind and value not in (None, ""):
            if isinstance(value, list):
                return [self.reference(item, kind=kind, source=source, subject_id=subject_id)
                        for item in value]
            return self.reference(value, kind=kind, source=source, subject_id=subject_id)
        if isinstance(value, dict):
            result = {}
            for name, child in value.items():
                binding = self._private_key(name, child, parent_key=key)
                child_subject = self._key_subject(name, child, source=source, subject_id=subject_id)
                projected_key = (self.reference(binding[1], kind=binding[0], source=source,
                                                subject_id=child_subject) if binding else name)
                result[projected_key] = self._project(child, key=self._child_key(key, name, value), source=source,
                                                      subject_id=child_subject)
            return result
        if isinstance(value, list):
            return [self._project(item, key=key, source=source, subject_id=subject_id)
                    for item in value]
        if isinstance(value, str):
            if source == "tool" and (banking := self._banking_text(value)) is not None:
                return banking
            return self.project_text(value, source=source, subject_id=subject_id)
        return value

    def resolve(self, value: Any, *, operation: str, now: datetime,
                subject_id: str | None, key: str = "") -> Any:
        if operation == "log_verification" and key == "name":
            key = "person_name"
        if isinstance(value, str) and _REFERENCE.fullmatch(value):
            record = self._record(value, now=now, subject_id=subject_id)
            if operation not in record["operations"] or private_kind(key) != record["kind"]:
                raise ProjectionDenied("PRIVATE_REFERENCE_KIND_OR_OPERATION_DENIED")
            return deepcopy(record["value"])
        if isinstance(value, dict):
            if private_kind(key):
                raise ProjectionDenied("PRIVATE_ARGUMENT_REQUIRES_REFERENCE")
            return {name: self.resolve(child, key=self._child_key(key, name), operation=operation,
                                       now=now, subject_id=subject_id)
                    for name, child in value.items()}
        if isinstance(value, list):
            return [self.resolve(child, key=key, operation=operation,
                                 now=now, subject_id=subject_id) for child in value]
        if isinstance(value, str) and "[[safe:" in value:
            raise ProjectionDenied("EMBEDDED_PRIVATE_REFERENCE_DENIED")
        if private_kind(key) and value not in (None, ""):
            if self._customer_scalar(value):
                kind = private_kind(key)
                assert kind is not None
                token = self.reference(value, kind=kind, source="customer",
                                       subject_id=subject_id, operations={operation})
                return self.resolve(token, key=key, operation=operation,
                                    now=now, subject_id=subject_id)
            raise ProjectionDenied("PRIVATE_ARGUMENT_REQUIRES_REFERENCE")
        return value

    def _customer_scalar(self, value: Any) -> bool:
        if type(value) not in (str, int):
            return False
        text = str(value)
        if not text or text != text.strip() or len(text.encode("utf-8")) > 1024:
            return False
        # Dots, signs, hyphens and @ remain token characters, avoiding partial
        # email addresses, account IDs, dates, ZIP+4 values or signed amounts.
        pattern = re.compile(r"(?<![\w@.+-])" + re.escape(text) + r"(?![\w@+-]|\.[\w@+-])")
        return any(pattern.search(message) is not None for message in self._customer_texts)

    def resolve_arguments(self, operation: str, arguments: dict[str, Any], *,
                          now: datetime, subject_id: str | None) -> dict[str, Any]:
        if operation == "call_discoverable_agent_tool":
            inner = arguments.get("agent_tool_name")
            raw = arguments.get("arguments")
            if (set(arguments) != {"agent_tool_name", "arguments"}
                    or not isinstance(inner, str) or not isinstance(raw, str)):
                raise ProjectionDenied("INVALID_NESTED_ARGUMENTS")
            parsed = strict_json(raw, max_bytes=65_536)
            if not isinstance(parsed, dict):
                raise ProjectionDenied("INVALID_NESTED_ARGUMENTS")
            # The outer operation is the allowlisted transport; the domain pack
            # separately checks the discovered inner name and exact schema.
            from safe_agent.contracts.episodes import canonical_arguments_json
            resolved = self.resolve(parsed, operation=operation, now=now, subject_id=subject_id)
            return {"agent_tool_name": inner, "arguments": canonical_arguments_json(resolved)}
        return self.resolve(arguments, operation=operation, now=now, subject_id=subject_id)

    def render(self, text: str, *, now: datetime, subject_id: str | None,
               authenticated: bool) -> str:
        def replace(match: re.Match[str]) -> str:
            record = self._record(match.group(), now=now, subject_id=subject_id)
            if (record["source"] != "customer" and not authenticated
                    and record["kind"] not in _PUBLIC_RESOURCE_KINDS):
                raise ProjectionDenied("RECIPIENT_NOT_AUTHENTICATED")
            if not isinstance(record["value"], str | int):
                raise ProjectionDenied("PRIVATE_RESPONSE_VALUE_NOT_SCALAR")
            return str(record["value"])
        rendered = _REFERENCE.sub(replace, text)
        if "[[safe:" in rendered:
            raise ProjectionDenied("UNRESOLVED_PRIVATE_RESPONSE_REFERENCE")
        return rendered


def identity_values(text: str) -> tuple[str, ...]:
    """Raw customer evidence, used only by trusted identity comparison code."""
    return (text,) if text.strip() else ()
