"""Privileged synthetic-only task assignment from operator-owned allowlists.

Task intake never accepts a manifest, tenant, protected resource ID, operation,
budget or approval policy from HTTP/model input. Real connectors are out of scope.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError

from safe_agent.broker.references import BrokerScope, Operation, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.base import Identifier, StrictModel, require_aware
from safe_agent.contracts.control import Task, TaskBudget, TaskManifest
from safe_agent.contracts.intake import (
    TaskIntakeReceipt,
    TaskIntakeRequest,
    TaskOptionsPage,
    TaskOptionView,
    Workflow,
)
from safe_agent.contracts.internal import InternalTaskManifest, InternalTaskRecord
from safe_agent.evidence.ledger import TaskSnapshot
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.security.principal import Principal
from safe_agent.storage.repository import LedgerRepository
from safe_agent.virtual_office.state import SyntheticOfficeState


class OptionUnavailable(ValueError):
    """Use a uniform not-found response for unknown or unauthorized options."""


class InvalidTaskWindow(ValueError):
    """A submitted window is outside the server-owned option policy."""


class TrustedSyntheticOption(StrictModel):
    """Private, operator-configured selection policy; never serialize via API."""

    option_id: Identifier
    organization_id: Identifier
    initiator_id: Identifier
    workflow: Workflow
    customer_resource_id: Identifier
    calendar_resource_id: Identifier | None = None
    appointment_template_id: Identifier | None = None
    task_code: Literal["call_back", "prepare_documents", "review_request"] | None = None
    time_zone: str = "UTC"
    requires_approval: bool = True
    max_ahead_days: int = Field(default=7, ge=1, le=30)
    max_window_minutes: int = Field(default=240, ge=15, le=480)
    max_duration_minutes: int = Field(default=60, ge=1, le=60)
    max_cycles: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def valid(self) -> TrustedSyntheticOption:
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("invalid option time zone") from error
        if self.workflow == "schedule_follow_up":
            if (self.calendar_resource_id is None
                    or self.appointment_template_id is None or self.task_code is not None):
                raise ValueError("calendar option requires exactly calendar/template resources")
        elif (self.calendar_resource_id is not None
              or self.appointment_template_id is not None or self.task_code is None):
            raise ValueError("internal task option cannot grant calendar resources")
        return self


class TrustedTaskProvisioner:
    """Only trusted installation code can supply synthetic source and option grants."""

    def __init__(
        self, coordinator: TrustedOfficeTasks, repo: LedgerRepository,
        broker: ReferenceBroker, source: SyntheticOfficeState,
        options: tuple[TrustedSyntheticOption, ...],
    ) -> None:
        self._coordinator = coordinator
        self._repo = repo
        self._broker = broker
        self._options: dict[str, TrustedSyntheticOption] = {}
        calendars = {c.calendar_id: c for c in source.calendars}
        customers = {c.customer_id for c in source.customers}
        templates = {t.template_id: t for t in source.appointment_templates}
        for option in options:
            if option.option_id in self._options:
                raise ValueError("duplicate task option")
            if (option.organization_id != source.organization_id
                    or option.customer_resource_id not in customers):
                raise ValueError("task option references wrong synthetic organization")
            if option.workflow == "schedule_follow_up":
                assert option.calendar_resource_id is not None
                assert option.appointment_template_id is not None
                calendar = calendars.get(option.calendar_resource_id)
                template = templates.get(option.appointment_template_id)
                if (calendar is None or template is None
                        or option.time_zone != calendar.time_zone
                        or template.duration_minutes > option.max_duration_minutes):
                    raise ValueError("invalid synthetic calendar option binding")
            self._options[option.option_id] = option

    def _permitted_option(self, principal: Principal, option_id: str) -> TrustedSyntheticOption:
        option = self._options.get(option_id)
        if (option is None or option.organization_id != principal.organization_id
                or option.initiator_id != principal.actor_id):
            raise OptionUnavailable("task option unavailable")
        return option

    def available_options(self, principal: Principal, *, now: datetime) -> TaskOptionsPage:
        principal.require("task:create", principal.organization_id, require_aware(now))
        visible = (
            TaskOptionView(
                option_id=option.option_id,
                workflow=option.workflow,
                display_name="Schedule follow-up" if option.workflow == "schedule_follow_up"
                else "Create internal task",
                time_zone=option.time_zone,
                requires_approval=option.requires_approval,
                max_ahead_days=option.max_ahead_days,
                max_window_minutes=option.max_window_minutes,
            )
            for option in sorted(self._options.values(), key=lambda x: x.option_id)
            if (option.organization_id == principal.organization_id
                and option.initiator_id == principal.actor_id)
        )
        return TaskOptionsPage(options=tuple(visible))

    @staticmethod
    def _receipt(snapshot: TaskSnapshot) -> TaskIntakeReceipt:
        return TaskIntakeReceipt(
            task_id=snapshot.task_id,
            phase=snapshot.phase,
            state_version=snapshot.state_version,
        )

    def create(
        self, principal: Principal, request: TaskIntakeRequest, *, now: datetime,
    ) -> TaskIntakeReceipt:
        now = require_aware(now)
        principal.require("task:create", principal.organization_id, now)
        option = self._permitted_option(principal, request.option_id)
        request_hash = sha256(request)
        previous = self._repo.find_task_intake(
            principal.organization_id, principal.actor_id, request.idempotency_key,
            request_hash=request_hash,
        )
        if previous is not None:
            return self._receipt(previous)
        if (request.allowed_start < now
                or request.allowed_end > now + timedelta(days=option.max_ahead_days)
                or request.allowed_end - request.allowed_start
                   > timedelta(minutes=option.max_window_minutes)):
            raise InvalidTaskWindow("task window outside assigned option scope")

        # IDs, budgets, approval policy and resource references are always server-generated.
        task_id = "task_" + secrets.token_hex(16)
        manifest_id = "manifest_" + secrets.token_hex(16)
        deadline = now + timedelta(hours=24)
        operation: Operation = ("calendar.create_event" if option.workflow == "schedule_follow_up"
                                else "tasks.create")
        scope = BrokerScope(
            organization_id=principal.organization_id,
            initiator_id=principal.actor_id,
            task_id=task_id, operation=operation, now=now,
        )
        customer_ref = self._broker.mint(
            resource_id=option.customer_resource_id,
            resource_kind=ResourceKind.CUSTOMER,
            scope=scope, permitted_operations=(operation,),
            expires_at=deadline,
        )
        budget = TaskBudget(max_cycles=option.max_cycles, max_external_effects=1)
        if option.workflow == "schedule_follow_up":
            assert option.calendar_resource_id is not None
            assert option.appointment_template_id is not None
            calendar_ref = self._broker.mint(
                resource_id=option.calendar_resource_id,
                resource_kind=ResourceKind.CALENDAR, scope=scope,
                permitted_operations=(operation,), expires_at=deadline,
            )
            manifest: TaskManifest | InternalTaskManifest = TaskManifest(
                manifest_id=manifest_id, manifest_version=1,
                organization_id=principal.organization_id,
                initiator_id=principal.actor_id, task_id=task_id,
                calendar_ref=calendar_ref, customer_ref=customer_ref,
                permitted_operations=("calendar.create_event",),
                allowed_start=request.allowed_start, allowed_end=request.allowed_end,
                time_zone=option.time_zone,
                template_id=option.appointment_template_id,
                max_duration_minutes=option.max_duration_minutes,
                requires_approval=option.requires_approval, budget=budget,
            )
            task: Task | InternalTaskRecord = Task(
                task_id=task_id, organization_id=principal.organization_id,
                initiator_id=principal.actor_id, manifest_id=manifest_id,
                manifest_hash=sha256(manifest), objective_code="schedule_follow_up",
                created_at=now, deadline=deadline,
            )
        else:
            assert option.task_code is not None
            manifest = InternalTaskManifest(
                manifest_id=manifest_id, manifest_version=1,
                organization_id=principal.organization_id,
                initiator_id=principal.actor_id, task_id=task_id,
                customer_ref=customer_ref,
                allowed_due_start=request.allowed_start,
                allowed_due_end=request.allowed_end,
                task_code=option.task_code,
                requires_approval=option.requires_approval, budget=budget,
            )
            task = InternalTaskRecord(
                task_id=task_id, organization_id=principal.organization_id,
                initiator_id=principal.actor_id, manifest_id=manifest_id,
                manifest_hash=sha256(manifest), created_at=now, deadline=deadline,
            )
        references = self._broker.trusted_export(task_id=task_id)
        try:
            snapshot = self._coordinator.register(
                principal, task, manifest, now=now,
                protected_references=references,
                intake_key=request.idempotency_key, intake_hash=request_hash,
            )
        except IntegrityError:
            # Concurrent duplicate submits race on the SQL unique intake key.
            # Only the matching, already-committed request may be returned.
            previous = self._repo.find_task_intake(
                principal.organization_id, principal.actor_id, request.idempotency_key,
                request_hash=request_hash,
            )
            if previous is None:
                raise
            snapshot = previous
        return self._receipt(snapshot)
