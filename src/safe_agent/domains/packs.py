"""Observation-bound admission for the frozen tau retail and banking domains.

These packs enforce a declared mechanical subset: closed signatures, effects,
customer identity, resource ownership, discovery routes, and action-bound retail
confirmation. Natural-language banking policy, customer intent, and response
privacy are separate host/model responsibilities; no decision certifies them.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pydantic import JsonValue

from safe_agent.canonical import sha256
from safe_agent.tal.domain_runtime import DomainAdmissionEvaluator

from ._catalog_data import CATALOG
from .contracts import (
    Capability,
    DomainContext,
    DomainDecision,
    DomainEffect,
    ToolObservation,
    ToolProposal,
)
from .schema import ArgumentSchemaError, parse_argument_object, validate_arguments

_RETAIL_LOOKUPS = {"find_user_id_by_email", "find_user_id_by_name_zip"}
_BANK_LOOKUPS = {
    "get_user_information_by_id",
    "get_user_information_by_name",
    "get_user_information_by_email",
}
_RETRIEVAL = {"KB_search", "KB_search_bm25", "KB_search_dense", "grep"}
_BANK_CONTROL = {
    "transfer_to_human_agents",
    "give_discoverable_user_tool",
    "unlock_discoverable_agent_tool",
    "list_discoverable_agent_tools",
    "get_current_time",
    "initial_transfer_to_human_agent_0218",
    "initial_transfer_to_human_agent_1822",
    "emergency_credit_bureau_incident_transfer_1114",
} | _RETRIEVAL
_IDENTITY_FIELDS = ("date_of_birth", "email", "phone_number", "address")
_BANK_RESOURCE_FIELDS = {
    "account_id",
    "checking_account_id",
    "credit_card_account_id",
    "source_account_id",
    "destination_account_id",
    "card_id",
    "transaction_id",
}
_BANK_TABLES = {
    "get_user_information_by_id": {"users"},
    "get_user_information_by_name": {"users"},
    "get_user_information_by_email": {"users"},
    "get_referrals_by_user": {"referrals"},
    "get_credit_card_transactions_by_user": {"credit_card_transaction_history"},
    "get_credit_card_accounts_by_user": {"credit_card_accounts"},
    "get_all_user_accounts_by_user_id_3847": {"accounts", "credit_card_accounts"},
    "get_bank_account_transactions_9173": {"bank_account_transaction_history"},
    "get_debit_dispute_status_7483": {"debit_card_disputes"},
    "get_user_dispute_history_7291": {"transaction_disputes"},
    "get_pending_replacement_orders_5765": {"credit_card_orders"},
    "get_closure_reason_history_8293": {"credit_card_closure_reasons"},
    "get_credit_limit_increase_history_4829": {"credit_limit_increase_requests"},
}
_BANK_RESULT_IDS = {
    "get_credit_card_accounts_by_user": ("account_id",),
    "get_all_user_accounts_by_user_id_3847": ("account_id",),
    "get_credit_card_transactions_by_user": ("transaction_id",),
    "get_bank_account_transactions_9173": ("transaction_id",),
}


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _user_supplied(value: Any, context: DomainContext) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = _normalized(value)
    if candidate in {_normalized(v) for v in context.user_provided_identity_values}:
        return True
    # Boundaries avoid matching a short ID as part of another identifier.
    pattern = r"(?<![\w@])" + re.escape(candidate) + r"(?![\w@])"
    return any(re.search(pattern, _normalized(message)) for message in context.user_messages)


def _result_data(result: JsonValue) -> JsonValue:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (ValueError, RecursionError):
            return result
    return result


def _successful(result: JsonValue, success: bool) -> bool:
    if not success:
        return False
    if isinstance(result, str) and re.match(r"\s*(error\b|failed\b)", result, re.IGNORECASE):
        return False
    return not (isinstance(result, dict) and result.get("error"))


def _record_value(text: str) -> JsonValue:
    if text.startswith(("{", "[")) and len(text) <= 65536:
        try:
            value = ast.literal_eval(text)
            # Do not retain sets, tuples, object instances, or non-finite floats.
            return json.loads(json.dumps(value, allow_nan=False))
        except (ValueError, TypeError, SyntaxError, RecursionError):
            pass
    return text


def _bank_records(text: str, allowed_tables: set[str]) -> list[tuple[str, dict[str, JsonValue]]]:
    """Decode the pinned query_database_tool presentation, never arbitrary prose.

    Only exact record headers/field lines inside declared tables are accepted.
    Duplicate fields invalidate a record. This parser grants no ownership itself.
    """
    if len(text) > 1_000_000:
        return []
    records: list[tuple[str, dict[str, JsonValue]]] = []
    table: str | None = None
    record_id: str | None = None
    fields: dict[str, JsonValue] = {}
    invalid = False

    def flush() -> None:
        if record_id and fields and not invalid:
            records.append((record_id, dict(fields)))

    for line in text.splitlines():
        header = re.fullmatch(r"Found (\d+) record\(s\) in '([A-Za-z_]+)':", line)
        if header:
            flush()
            table = header[2] if header[2] in allowed_tables else None
            record_id, fields, invalid = None, {}, False
            continue
        record = re.fullmatch(r"\d+\. Record ID: ([A-Za-z0-9_.:#-]+)", line)
        if record:
            flush()
            record_id = record[1] if table else None
            fields, invalid = {}, False
            continue
        field = re.fullmatch(r"   ([A-Za-z_][A-Za-z0-9_]*): (.*)", line)
        if record_id and field:
            if field[1] in fields:
                invalid = True
            fields[field[1]] = _record_value(field[2])
        elif record_id and line.strip():
            flush()
            table, record_id, fields, invalid = None, None, {}, False
    flush()
    return records


class DomainPack:
    """Trusted catalog + pure admission + observed-state reduction; no dispatch."""

    def __init__(
        self,
        name: str,
        tool_schemas: Any = None,
        *,
        admission_evaluator: DomainAdmissionEvaluator | None = None,
    ) -> None:
        catalog = cast(dict[str, Any], CATALOG)
        if name not in catalog["domains"]:
            raise ValueError("Unsupported benchmark domain")
        self.name = name
        if admission_evaluator is None:
            from safe_agent.tal.domain_runtime import load_reviewed_domain_evaluator

            admission_evaluator = load_reviewed_domain_evaluator()
        self._admission_evaluator = admission_evaluator
        self.upstream_commit = catalog["upstream_commit"]
        self._catalog = {
            key: self._requirements(Capability.model_validate(copy.deepcopy(value)))
            for key, value in catalog["domains"][name].items()
        }
        self._schemas: dict[str, dict[str, Any]] = {}
        if tool_schemas is None:
            self._visible = {
                key
                for key, cap in self._catalog.items()
                if cap.actor == "agent" and not cap.discoverable
            }
        else:
            entries: Iterable[Any]
            if isinstance(tool_schemas, Mapping):
                entries = ((name, value) for name, value in tool_schemas.items())
            else:
                entries = ((spec.name, spec.parameters) for spec in tool_schemas)
            self._visible = set()
            for tool_name, schema in entries:
                cap = self._catalog.get(tool_name)
                if cap is None or cap.actor != "agent" or cap.discoverable:
                    raise ValueError("Tool set contains an unsupported agent capability")
                if not isinstance(schema, dict):
                    schema = schema.parameters
                self._visible.add(tool_name)
                self._schemas[tool_name] = copy.deepcopy(schema)
        if not self._visible:
            raise ValueError("Domain requires at least one public tool")

    @property
    def capabilities(self) -> dict[str, Capability]:
        """Visible tool metadata only; undiscovered inner capabilities stay private."""
        return {name: self._catalog[name].model_copy(deep=True) for name in sorted(self._visible)}

    def _requirements(self, cap: Capability) -> Capability:
        if self.name == "retail":
            private = cap.name not in _RETAIL_LOOKUPS | {
                "calculate",
                "get_product_details",
                "get_item_details",
                "list_all_product_types",
                "transfer_to_human_agents",
            }
            return cap.model_copy(
                update={
                    "requires_subject": private,
                    "requires_authentication": private,
                    "requires_customer_confirmation": cap.effect == "write",
                }
            )
        private = cap.name not in _BANK_LOOKUPS | _BANK_CONTROL | {"call_discoverable_agent_tool"}
        return cap.model_copy(
            update={
                "requires_subject": private,
                "requires_authentication": private and cap.name != "log_verification",
            }
        )

    def _resolve(
        self, proposal: ToolProposal, context: DomainContext
    ) -> tuple[Capability, dict[str, Any]]:
        if proposal.domain != self.name or context.domain != self.name:
            raise ArgumentSchemaError("DOMAIN_MISMATCH")
        if proposal.tool_name not in self._visible:
            raise ArgumentSchemaError("DOMAIN_TOOL_NOT_EXPOSED")
        outer = self._catalog[proposal.tool_name]
        validate_arguments(proposal.arguments, outer.parameters)
        if proposal.tool_name in self._schemas:
            validate_arguments(proposal.arguments, self._schemas[proposal.tool_name])
        outer_args: dict[str, Any] = dict(proposal.arguments)
        if proposal.tool_name in {"call_discoverable_agent_tool", "unlock_discoverable_agent_tool"}:
            inner_name = outer_args["agent_tool_name"]
            cap = self._catalog.get(inner_name)
            if cap is None or cap.actor != "agent" or not cap.discoverable:
                raise ArgumentSchemaError("DOMAIN_DISCOVERY_TARGET_INVALID")
            if inner_name not in context.discovered_tools:
                raise ArgumentSchemaError("DOMAIN_TOOL_NOT_DISCOVERED")
            if proposal.tool_name == "call_discoverable_agent_tool":
                if inner_name not in context.unlocked_tools:
                    raise ArgumentSchemaError("DOMAIN_TOOL_NOT_UNLOCKED")
                args = parse_argument_object(outer_args.get("arguments", "{}"))
                validate_arguments(args, cap.parameters)
                return cap, args
        if proposal.tool_name == "give_discoverable_user_tool":
            inner_name = outer_args["discoverable_tool_name"]
            cap = self._catalog.get(inner_name)
            if cap is None or cap.actor != "user" or not cap.discoverable:
                raise ArgumentSchemaError("DOMAIN_USER_TOOL_TARGET_INVALID")
            if inner_name not in context.discovered_tools:
                raise ArgumentSchemaError("DOMAIN_TOOL_NOT_DISCOVERED")
            args = parse_argument_object(outer_args.get("arguments", "{}"))
            validate_arguments(args, cap.parameters, partial=True)
            if reason := self._bank_scope(args, context):
                raise ArgumentSchemaError(reason)
        return outer, outer_args

    def admit(self, proposal: ToolProposal, context: DomainContext) -> DomainDecision:
        from tal.domain_admission import (  # type: ignore[import-untyped, import-not-found]
            DomainActionProposal,
            DomainAdmissionStatus,
            DomainCapability,
            DomainCustomerConfirmation,
            DomainGrantDraft,
            TrustedDomainEvidence,
            compile_domain_grant,
        )

        # Pydantic model_copy/update and nested dict mutation are not validation.
        proposal = ToolProposal.model_validate(proposal.model_dump())
        effective = proposal.tool_name
        effect: DomainEffect = "control"
        try:
            cap, args = self._resolve(proposal, context)
            effective = cap.name
            # The upstream dispatch wrapper can write its call log even for an
            # inner read. Its exact retry classification is therefore write.
            effect = "write" if proposal.tool_name == "call_discoverable_agent_tool" else cap.effect
            reason = (
                self._retail_checks(cap, args, context)
                if self.name == "retail"
                else self._bank_checks(cap, args, context)
            )
        except ArgumentSchemaError as exc:
            reason = (
                str(exc) if str(exc).startswith("DOMAIN_") else "DOMAIN_ARGUMENT_SCHEMA_INVALID"
            )
        if reason:
            return DomainDecision(
                allowed=False,
                reason_code=reason,
                effect=effect,
                proposal_digest=proposal.digest,
                effective_tool_name=effective,
            )

        capabilities = []
        inner_names = tuple(
            sorted(
                name
                for name, item in self._catalog.items()
                if item.discoverable and item.actor == "agent"
            )
        )
        for name, item in self._catalog.items():
            if item.actor != "agent" or (name not in self._visible and not item.discoverable):
                continue
            capabilities.append(
                DomainCapability(
                    operation=name,
                    effect=item.effect,
                    requires_subject=item.requires_subject,
                    requires_authentication=item.requires_authentication,
                    requires_customer_confirmation=item.requires_customer_confirmation,
                    directly_callable=name in self._visible,
                    dispatches=inner_names if name == "call_discoverable_agent_tool" else (),
                )
            )
        grant = compile_domain_grant(
            DomainGrantDraft(
                episode_id=context.episode_id,
                domain=context.domain,
                policy_hash=context.policy_hash,
                capabilities=tuple(capabilities),
                max_tool_calls=context.max_tool_calls,
            )
        )
        consent = context.consent
        confirmation = (
            None
            if consent is None
            else DomainCustomerConfirmation(
                episode_id=consent.episode_id,
                subject_id=consent.subject_id,
                policy_hash=consent.policy_hash,
                effect_hash=consent.proposal_digest,
                user_turn_id=consent.user_turn_id,
            )
        )
        decision = self._admission_evaluator(
            grant,
            DomainActionProposal(
                episode_id=context.episode_id,
                domain=context.domain,
                policy_hash=context.policy_hash,
                operation=proposal.tool_name,
                effective_operation=effective,
                arguments_json=proposal.arguments_json,
                arguments_hash=proposal.arguments_hash,
                subject_id=context.subject_id,
            ),
            TrustedDomainEvidence(
                episode_id=context.episode_id,
                domain=context.domain,
                policy_hash=context.policy_hash,
                observation_hash=context.observation_hash,
                expected_observation_hash=context.observation_hash,
                state_version=context.state_version,
                expected_state_version=context.state_version,
                subject_id=context.subject_id,
                authenticated=context.authenticated,
                effective_operation=effective,
                mechanical_checks_passed=True,
                scope_valid=True,
                tool_calls_used=context.tool_calls_used,
                unresolved_write=context.unresolved_write,
                latest_user_turn_id=context.latest_user_turn_id,
                confirmation=confirmation,
            ),
        )
        return DomainDecision(
            allowed=decision.status == DomainAdmissionStatus.ELIGIBLE,
            reason_code=(
                "CUSTOMER_CONSENT_REQUIRED"
                if decision.status == DomainAdmissionStatus.CONFIRMATION_REQUIRED
                else decision.reason_code
            ),
            effect=effect,
            proposal_digest=proposal.digest,
            effective_tool_name=effective,
            tal_decision_hash=sha256(decision.model_dump(mode="json")),
            requires_customer_confirmation=decision.status
            == DomainAdmissionStatus.CONFIRMATION_REQUIRED,
        )

    def observe_user(
        self,
        content: str,
        context: DomainContext,
        *,
        user_turn_id: str,
        identity_values: tuple[str, ...] = (),
    ) -> DomainContext:
        """Record actual inbound text. This function cannot mint customer consent."""
        return context.model_copy(
            update={
                "user_messages": (*context.user_messages, content),
                "user_provided_identity_values": (
                    *context.user_provided_identity_values,
                    *identity_values,
                ),
                "latest_user_turn_id": user_turn_id,
                "consent": None,
            },
            deep=True,
        )

    def observe(
        self,
        proposal: ToolProposal,
        result: JsonValue,
        context: DomainContext,
        *,
        success: bool = True,
    ) -> DomainContext:
        """Reduce a correlated tool result; the trusted host supplies correlation.

        The host owns observation hashes/versioning and may replace call counts
        with its durable outbox count. No missing/failed response is a success.
        """
        success = _successful(result, success)
        updated = context.model_copy(
            update={
                "observations": (
                    *context.observations,
                    ToolObservation(
                        tool_name=proposal.tool_name,
                        arguments=copy.deepcopy(proposal.arguments),
                        result=copy.deepcopy(result),
                        success=success,
                    ),
                ),
                "tool_calls_used": context.tool_calls_used + 1,
            },
            deep=True,
        )
        if not success:
            return updated
        try:
            cap, args = self._resolve(proposal, context)
        except ArgumentSchemaError:
            return updated
        if self.name == "retail":
            return self._observe_retail(cap.name, args, _result_data(result), updated)
        return self._observe_banking(cap.name, args, result, updated)

    def _retail_checks(
        self, cap: Capability, args: dict[str, Any], context: DomainContext
    ) -> str | None:
        if cap.name in _RETAIL_LOOKUPS:
            if context.subject_id:
                return "DOMAIN_CUSTOMER_ALREADY_IDENTIFIED"
            if not all(_user_supplied(value, context) for value in args.values()):
                return "DOMAIN_CUSTOMER_IDENTITY_INPUT_REQUIRED"
        if "user_id" in args and args["user_id"] != context.subject_id:
            return "DOMAIN_SUBJECT_MISMATCH"
        order: dict[str, Any] | None = None
        if "order_id" in args:
            key = "order:" + args["order_id"]
            if context.entity_owners.get(key) != context.subject_id or context.subject_id is None:
                return "DOMAIN_ORDER_OWNERSHIP_REQUIRED"
            observed = context.entities.get(key)
            if isinstance(observed, dict):
                order = dict(observed)
            if cap.effect == "write" and order is None:
                return "DOMAIN_ORDER_OBSERVATION_REQUIRED"
        if cap.effect != "write" or order is None:
            return None
        status = order.get("status")
        if (
            cap.name in {"cancel_pending_order", "modify_pending_order_items"}
            and status != "pending"
        ):
            return "DOMAIN_ORDER_STATUS_INVALID"
        if cap.name in {
            "modify_pending_order_address",
            "modify_pending_order_payment",
        } and status not in {
            "pending",
            "pending (item modified)",
        }:
            return "DOMAIN_ORDER_STATUS_INVALID"
        if (
            cap.name in {"return_delivered_order_items", "exchange_delivered_order_items"}
            and status != "delivered"
        ):
            return "DOMAIN_ORDER_STATUS_INVALID"
        if cap.name == "cancel_pending_order" and args["reason"] not in {
            "no longer needed",
            "ordered by mistake",
        }:
            return "DOMAIN_CANCELLATION_REASON_INVALID"
        items = order.get("items", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            return "DOMAIN_ORDER_ITEMS_OBSERVATION_INVALID"
        if "item_ids" in args:
            requested = args["item_ids"]
            available = Counter(item.get("item_id") for item in items)
            if not requested or Counter(requested) - available:
                return "DOMAIN_ORDER_ITEMS_INVALID"
        difference = Decimal(0)
        if "new_item_ids" in args:
            if len(args["new_item_ids"]) != len(args["item_ids"]):
                return "DOMAIN_REPLACEMENT_COUNT_INVALID"
            for old_id, new_id in zip(args["item_ids"], args["new_item_ids"], strict=True):
                old = next(item for item in items if item.get("item_id") == old_id)
                new = context.entities.get("item:" + new_id)
                if (
                    not isinstance(new, dict)
                    or new.get("available") is not True
                    or new.get("product_id") != old.get("product_id")
                ):
                    return "DOMAIN_REPLACEMENT_VARIANT_UNVERIFIED"
                try:
                    difference += Decimal(str(new["price"])) - Decimal(str(old["price"]))
                except (KeyError, InvalidOperation):
                    return "DOMAIN_PRICE_OBSERVATION_REQUIRED"
        if "payment_method_id" in args:
            user = context.entities.get("user:" + str(context.subject_id), {})
            methods = user.get("payment_methods", {}) if isinstance(user, dict) else {}
            if not isinstance(methods, dict):
                return "DOMAIN_PAYMENT_OWNERSHIP_REQUIRED"
            method = methods.get(args["payment_method_id"])
            if not isinstance(method, dict):
                return "DOMAIN_PAYMENT_OWNERSHIP_REQUIRED"
            history = order.get("payment_history", [])
            if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
                return "DOMAIN_PAYMENT_OBSERVATION_INVALID"
            if cap.name == "return_delivered_order_items":
                if method.get("source") != "gift_card" and (
                    not history or history[0].get("payment_method_id") != args["payment_method_id"]
                ):
                    return "DOMAIN_REFUND_METHOD_INVALID"
            if cap.name == "modify_pending_order_payment":
                if (
                    len(history) != 1
                    or history[0].get("transaction_type") != "payment"
                    or history[0].get("payment_method_id") == args["payment_method_id"]
                ):
                    return "DOMAIN_PAYMENT_CHANGE_INVALID"
                try:
                    difference = Decimal(str(history[0]["amount"]))
                except (KeyError, InvalidOperation):
                    return "DOMAIN_PRICE_OBSERVATION_REQUIRED"
            if method.get("source") == "gift_card":
                try:
                    balance = Decimal(str(method["balance"]))
                    if (
                        not balance.is_finite()
                        or not difference.is_finite()
                        or balance < difference
                    ):
                        return "DOMAIN_GIFT_CARD_BALANCE_INSUFFICIENT"
                except (KeyError, InvalidOperation):
                    return "DOMAIN_BALANCE_OBSERVATION_REQUIRED"
        return None

    def _observe_retail(
        self,
        name: str,
        args: dict[str, Any],
        result: JsonValue,
        context: DomainContext,
    ) -> DomainContext:
        entities, owners = copy.deepcopy(context.entities), dict(context.entity_owners)
        changes: dict[str, Any] = {}
        if name in _RETAIL_LOOKUPS and not context.subject_id:
            if (
                isinstance(result, str)
                and re.fullmatch(r"[A-Za-z0-9_]+", result)
                and all(_user_supplied(v, context) for v in args.values())
            ):
                changes.update(subject_id=result, authenticated=True)
        if isinstance(result, dict):
            data: dict[str, Any] = dict(result)
            if (
                name in {"get_user_details", "modify_user_address"}
                and context.subject_id is not None
                and data.get("user_id") == context.subject_id
            ):
                entities["user:" + context.subject_id] = data
                orders = data.get("orders", [])
                for order_id in orders if isinstance(orders, list) else []:
                    if isinstance(order_id, str):
                        owners["order:" + order_id] = context.subject_id
            elif "order_id" in args and data.get("order_id") == args["order_id"]:
                if context.subject_id is not None and data.get("user_id") == context.subject_id:
                    entities["order:" + args["order_id"]] = data
                    owners["order:" + args["order_id"]] = context.subject_id
            elif name == "get_product_details" and data.get("product_id") == args["product_id"]:
                entities["product:" + args["product_id"]] = data
                variants = data.get("variants", {})
                for key, item in variants.items() if isinstance(variants, dict) else ():
                    if isinstance(item, dict) and item.get("item_id") == key:
                        entities["item:" + key] = {**item, "product_id": args["product_id"]}
        changes.update(entities=entities, entity_owners=owners)
        return context.model_copy(update=changes, deep=True)

    def _bank_scope(self, args: dict[str, Any], context: DomainContext) -> str | None:
        if "user_id" in args and args["user_id"] != context.subject_id:
            return "DOMAIN_SUBJECT_MISMATCH"
        for name in _BANK_RESOURCE_FIELDS & args.keys():
            identifier = args[name]
            if (
                not isinstance(identifier, str)
                or context.subject_id is None
                or context.entity_owners.get("bank:" + identifier) != context.subject_id
            ):
                return "DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED"
        return None

    def _verification_reason(self, args: dict[str, Any], context: DomainContext) -> str | None:
        user = context.entities.get("user:" + str(context.subject_id))
        if not isinstance(user, dict):
            return "DOMAIN_CUSTOMER_RECORD_REQUIRED"
        for field in ("name", "user_id", *_IDENTITY_FIELDS):
            value = user.get(field)
            if not isinstance(value, str) or _normalized(args[field]) != _normalized(value):
                return "DOMAIN_VERIFICATION_RECORD_MISMATCH"
        if sum(_user_supplied(user[field], context) for field in _IDENTITY_FIELDS) < 2:
            return "DOMAIN_TWO_CUSTOMER_IDENTITY_FIELDS_REQUIRED"
        if args["time_verified"] != context.entities.get("bank:current_time"):
            return "DOMAIN_OBSERVED_TIME_REQUIRED"
        return None

    def _bank_checks(
        self, cap: Capability, args: dict[str, Any], context: DomainContext
    ) -> str | None:
        if cap.name in _BANK_LOOKUPS:
            if not all(_user_supplied(value, context) for value in args.values()):
                return "DOMAIN_CUSTOMER_IDENTITY_INPUT_REQUIRED"
            if context.subject_id:
                user = context.entities.get("user:" + context.subject_id, {})
                field = {
                    "get_user_information_by_id": "user_id",
                    "get_user_information_by_name": "name",
                    "get_user_information_by_email": "email",
                }[cap.name]
                if not isinstance(user, dict) or _normalized(str(user.get(field))) != _normalized(
                    next(iter(args.values()))
                ):
                    return "DOMAIN_SUBJECT_MISMATCH"
            return None
        if reason := self._bank_scope(args, context):
            return reason
        if cap.name == "log_verification":
            return self._verification_reason(args, context)
        return None

    def _observe_banking(
        self,
        name: str,
        args: dict[str, Any],
        result: JsonValue,
        context: DomainContext,
    ) -> DomainContext:
        entities, owners = copy.deepcopy(context.entities), dict(context.entity_owners)
        changes: dict[str, Any] = {}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if name in _RETRIEVAL:
            found = {
                key
                for key, cap in self._catalog.items()
                if cap.discoverable and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text)
            }
            changes["discovered_tools"] = tuple(sorted(set(context.discovered_tools) | found))
        if name == "unlock_discoverable_agent_tool" and text.startswith(
            "Tool unlocked: " + args["agent_tool_name"] + "\n"
        ):
            changes["unlocked_tools"] = tuple(
                sorted(set(context.unlocked_tools) | {args["agent_tool_name"]})
            )
        if name == "get_current_time":
            match = re.fullmatch(
                r"The current time is (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]+)\.", text
            )
            if match:
                entities["bank:current_time"] = match[1]
        records = _bank_records(text, _BANK_TABLES.get(name, set()))
        if name in _BANK_LOOKUPS and len(records) == 1:
            record_id, user = records[0]
            user_id = user.get("user_id")
            if (
                isinstance(user_id, str)
                and user_id == record_id
                and context.subject_id in (None, user_id)
                and all(_user_supplied(v, context) for v in args.values())
            ):
                changes["subject_id"] = user_id
                entities["user:" + user_id] = user
        elif name in _BANK_TABLES and context.subject_id:
            # Grant only records returned by an already scoped authoritative read.
            if not self._bank_scope(args, context):
                for record_id, record in records:
                    explicit_owner = record.get("user_id", record.get("referrer_id"))
                    if explicit_owner is not None and explicit_owner != context.subject_id:
                        continue
                    owners["bank:" + record_id] = context.subject_id
                    entities["bank:" + record_id] = record
                    # Public business IDs may differ from the database row key.
                    # Only the pinned read's declared ID fields can create aliases.
                    for field in _BANK_RESULT_IDS.get(name, ()):
                        resource_id = record.get(field)
                        if isinstance(resource_id, str) and resource_id:
                            owners["bank:" + resource_id] = context.subject_id
                            entities["bank:" + resource_id] = record
        if (
            name == "get_debit_cards_by_account_id_7823"
            and context.subject_id is not None
            and not self._bank_scope(args, context)
        ):
            cards = _result_data(result)
            if isinstance(cards, list):
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    card_id = card.get("card_id")
                    if isinstance(card_id, str) and card.get("account_id") == args["account_id"]:
                        owners["bank:" + card_id] = context.subject_id
                        entities["bank:" + card_id] = card
        if (
            name == "log_verification"
            and text.startswith("Verification logged successfully.\n")
            and not self._verification_reason(args, context)
        ):
            changes["authenticated"] = True
        if name == "change_user_email" and text.startswith("Email updated successfully.\n"):
            updated_user = entities.get("user:" + str(context.subject_id))
            if isinstance(updated_user, dict) and args["user_id"] == context.subject_id:
                updated_user["email"] = args["new_email"]
        changes.update(entities=entities, entity_owners=owners)
        return context.model_copy(update=changes, deep=True)


def get_domain_pack(
    name: str,
    *,
    tool_schemas: Any = None,
    admission_evaluator: DomainAdmissionEvaluator | None = None,
) -> DomainPack:
    """Use reviewed TAL by default; explicit pure injection is for unit fixtures."""
    return DomainPack(name, tool_schemas=tool_schemas, admission_evaluator=admission_evaluator)
