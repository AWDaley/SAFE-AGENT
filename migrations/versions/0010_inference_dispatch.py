"""M7-05a immutable invocation bindings and hash-linked lifecycle schema.

Revision ID: 0010_inference_dispatch
Revises: 0009_inference_cost_accounts

This migration does NOT enable admission, dispatch or live providers. Existing
0001-0009 revisions remain unchanged. PostgreSQL identity/event triggers are
in addition to portable foreign-key, uniqueness and row-shape constraints.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_inference_dispatch"
down_revision = "0009_inference_cost_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('uq_safe_tasks_inference_binding',
                    'safe_tasks',
                    ['task_id', 'organization_id', 'manifest_hash'], unique=True)
    op.create_index('uq_safe_planning_attempt_inference_binding',
                    'safe_planning_attempts',
                    ['attempt_id', 'task_id', 'manifest_hash', 'task_state_version', 'lease_until'], unique=True)
    op.create_index('uq_safe_inference_reservation_binding',
                    'safe_inference_budget_reservations',
                    ['request_id', 'organization_id', 'task_id', 'route_id', 'model_revision', 'prompt_digest', 'deadline_at'], unique=True)
    op.create_table(
        "safe_inference_invocations",
        sa.Column('request_id', sa.String(128), primary_key=True, nullable=False),
        sa.Column('organization_id', sa.String(128), nullable=False),
        sa.Column('task_id', sa.String(128), nullable=False),
        sa.Column('attempt_id', sa.String(128), nullable=False),
        sa.Column('task_state_version', sa.Integer(), nullable=False),
        sa.Column('workspace_version', sa.Integer(), nullable=False),
        sa.Column('manifest_hash', sa.String(72), nullable=False),
        sa.Column('route_id', sa.String(128), nullable=False),
        sa.Column('model_revision', sa.String(128), nullable=False),
        sa.Column('prompt_digest', sa.String(72), nullable=False),
        sa.Column('request_digest', sa.String(72), nullable=False),
        sa.Column('binding_hash', sa.String(72), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('state_version', sa.Integer(), nullable=False),
        sa.Column('claim_version', sa.Integer(), nullable=False),
        sa.Column('claim_owner', sa.String(128), nullable=True),
        sa.Column('claim_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dispatch_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('proposal_hash', sa.String(72), nullable=True),
        sa.Column('reason_code', sa.String(32), nullable=True),
        sa.Column('event_sequence', sa.Integer(), nullable=False),
        sa.Column('latest_event_hash', sa.String(72), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['task_id', 'organization_id', 'manifest_hash'],
            ['safe_tasks.task_id', 'safe_tasks.organization_id', 'safe_tasks.manifest_hash'],
            name='fk_infer_invocation_task',
        ),
        sa.ForeignKeyConstraint(
            ['attempt_id', 'task_id', 'manifest_hash', 'task_state_version', 'lease_expires_at'],
            ['safe_planning_attempts.attempt_id', 'safe_planning_attempts.task_id', 'safe_planning_attempts.manifest_hash', 'safe_planning_attempts.task_state_version', 'safe_planning_attempts.lease_until'],
            name='fk_infer_invocation_attempt',
        ),
        sa.ForeignKeyConstraint(
            ['request_id', 'organization_id', 'task_id', 'route_id', 'model_revision', 'prompt_digest', 'deadline_at'],
            ['safe_inference_budget_reservations.request_id', 'safe_inference_budget_reservations.organization_id', 'safe_inference_budget_reservations.task_id', 'safe_inference_budget_reservations.route_id', 'safe_inference_budget_reservations.model_revision', 'safe_inference_budget_reservations.prompt_digest', 'safe_inference_budget_reservations.deadline_at'],
            name='fk_infer_invocation_reservation',
        ),
        sa.UniqueConstraint("organization_id", "task_id", "attempt_id",
                            name="uq_infer_invocation_attempt"),
        sa.UniqueConstraint("request_id", "binding_hash", name="uq_infer_invocation_identity"),
        sa.CheckConstraint(
            "state IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED', "
            "'FAILED_BEFORE_DISPATCH', 'UNKNOWN')",
            name='ck_infer_invocation_state',
        ),
        sa.CheckConstraint(
            'task_state_version >= 0 AND workspace_version >= 1 AND state_version >= 0 AND '
            'claim_version >= 0',
            name='ck_infer_invocation_versions',
        ),
        sa.CheckConstraint(
            'event_sequence >= 1 AND state_version = event_sequence - 1',
            name='ck_infer_invocation_sequence',
        ),
        sa.CheckConstraint(
            'issued_at < deadline_at AND deadline_at <= lease_expires_at AND updated_at >= '
            'issued_at',
            name='ck_infer_invocation_deadline',
        ),
        sa.CheckConstraint(
            '((claim_owner IS NULL AND claim_until IS NULL) OR (claim_owner IS NOT NULL AND '
            'claim_until IS NOT NULL AND claim_version > 0 AND issued_at < claim_until AND '
            'claim_until <= deadline_at))',
            name='ck_infer_invocation_claim',
        ),
        sa.CheckConstraint(
            "((state IN ('RESERVED', 'FAILED_BEFORE_DISPATCH') AND dispatch_started_at IS "
            "NULL) OR (state IN ('DISPATCH_STARTED', 'RESPONSE_VALIDATED', 'UNKNOWN') AND "
            'dispatch_started_at IS NOT NULL AND issued_at <= dispatch_started_at AND '
            'dispatch_started_at < deadline_at AND dispatch_started_at <= updated_at))',
            name='ck_infer_invocation_dispatch',
        ),
        sa.CheckConstraint(
            "((state = 'RESPONSE_VALIDATED' AND proposal_hash IS NOT NULL) OR (state <> "
            "'RESPONSE_VALIDATED' AND proposal_hash IS NULL))",
            name='ck_infer_invocation_result',
        ),
        sa.CheckConstraint(
            "((state = 'FAILED_BEFORE_DISPATCH' AND reason_code IS NOT NULL AND reason_code "
            "IN ('AUTHORITY_EXPIRED', 'CANCELLED_BEFORE_DISPATCH', "
            "'INFRASTRUCTURE_UNAVAILABLE')) OR (state = 'UNKNOWN' AND reason_code IS NOT NULL "
            "AND reason_code IN ('DISPATCH_OUTCOME_UNKNOWN', 'RESPONSE_REJECTED')) OR (state "
            "IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED') AND reason_code IS "
            'NULL))',
            name='ck_infer_invocation_reason',
        ),
    )
    op.create_index("ix_safe_inference_invocations_recovery",
                    "safe_inference_invocations", ["state", "deadline_at"])
    op.create_table(
        "safe_inference_invocation_events",
        sa.Column('request_id', sa.String(128), primary_key=True, nullable=False),
        sa.Column('sequence', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('binding_hash', sa.String(72), nullable=False),
        sa.Column('previous_state', sa.String(32), nullable=True),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason_code', sa.String(32), nullable=True),
        sa.Column('proposal_hash', sa.String(72), nullable=True),
        sa.Column('preceding_hash', sa.String(72), nullable=True),
        sa.Column('event_hash', sa.String(72), nullable=False),
        sa.ForeignKeyConstraint(
            ['request_id', 'binding_hash'],
            ['safe_inference_invocations.request_id', 'safe_inference_invocations.binding_hash'],
            name='fk_infer_invocation_event_binding',
        ),
        sa.CheckConstraint(
            'sequence >= 1',
            name='ck_infer_invocation_event_sequence',
        ),
        sa.CheckConstraint(
            '((sequence = 1 AND previous_state IS NULL AND preceding_hash IS NULL AND state = '
            "'RESERVED') OR (sequence > 1 AND previous_state IS NOT NULL AND preceding_hash "
            'IS NOT NULL))',
            name='ck_infer_invocation_event_order',
        ),
        sa.CheckConstraint(
            "((previous_state IS NULL AND state = 'RESERVED') OR (previous_state = 'RESERVED' "
            "AND state IN ('DISPATCH_STARTED', 'FAILED_BEFORE_DISPATCH')) OR (previous_state "
            "= 'DISPATCH_STARTED' AND state IN ('RESPONSE_VALIDATED', 'UNKNOWN')))",
            name='ck_infer_invocation_event_transition',
        ),
        sa.CheckConstraint(
            "((state = 'RESPONSE_VALIDATED' AND proposal_hash IS NOT NULL) OR (state <> "
            "'RESPONSE_VALIDATED' AND proposal_hash IS NULL))",
            name='ck_infer_invocation_event_result',
        ),
        sa.CheckConstraint(
            "((state = 'FAILED_BEFORE_DISPATCH' AND reason_code IS NOT NULL AND reason_code "
            "IN ('AUTHORITY_EXPIRED', 'CANCELLED_BEFORE_DISPATCH', "
            "'INFRASTRUCTURE_UNAVAILABLE')) OR (state = 'UNKNOWN' AND reason_code IS NOT NULL "
            "AND reason_code IN ('DISPATCH_OUTCOME_UNKNOWN', 'RESPONSE_REJECTED')) OR (state "
            "IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED') AND reason_code IS "
            'NULL))',
            name='ck_infer_invocation_event_reason',
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION safe_guard_inference_invocation() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.state <> 'RESERVED' OR NEW.state_version <> 0
                       OR NEW.event_sequence <> 1 OR NEW.claim_version <> 0
                       OR NEW.claim_owner IS NOT NULL OR NEW.claim_until IS NOT NULL THEN
                        RAISE EXCEPTION 'INVOCATION_INITIAL_STATE_DENIED';
                    END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'INVOCATION_HISTORY_IMMUTABLE';
                END IF;
                IF ROW(OLD.request_id, OLD.organization_id, OLD.task_id, OLD.attempt_id, OLD.task_state_version, OLD.workspace_version, OLD.manifest_hash, OLD.route_id, OLD.model_revision, OLD.prompt_digest, OLD.request_digest, OLD.binding_hash, OLD.issued_at, OLD.deadline_at, OLD.lease_expires_at)
                   IS DISTINCT FROM ROW(NEW.request_id, NEW.organization_id, NEW.task_id, NEW.attempt_id, NEW.task_state_version, NEW.workspace_version, NEW.manifest_hash, NEW.route_id, NEW.model_revision, NEW.prompt_digest, NEW.request_digest, NEW.binding_hash, NEW.issued_at, NEW.deadline_at, NEW.lease_expires_at) THEN
                    RAISE EXCEPTION 'INVOCATION_BINDING_IMMUTABLE';
                END IF;
                IF OLD.dispatch_started_at IS NOT NULL
                   AND NEW.dispatch_started_at IS DISTINCT FROM OLD.dispatch_started_at THEN
                    RAISE EXCEPTION 'INVOCATION_DISPATCH_MARKER_IMMUTABLE';
                END IF;
                IF NEW.updated_at < OLD.updated_at OR NEW.claim_version < OLD.claim_version THEN
                    RAISE EXCEPTION 'INVOCATION_VERSION_REGRESSION';
                END IF;
                IF NEW.state <> OLD.state THEN
                    IF NOT ((OLD.state = 'RESERVED' AND NEW.state IN
                             ('DISPATCH_STARTED', 'FAILED_BEFORE_DISPATCH'))
                            OR (OLD.state = 'DISPATCH_STARTED' AND NEW.state IN
                                ('RESPONSE_VALIDATED', 'UNKNOWN')))
                       OR NEW.state_version <> OLD.state_version + 1
                       OR NEW.event_sequence <> OLD.event_sequence + 1
                       OR NEW.latest_event_hash = OLD.latest_event_hash THEN
                        RAISE EXCEPTION 'INVOCATION_TRANSITION_DENIED';
                    END IF;
                ELSIF ROW(NEW.state_version, NEW.event_sequence, NEW.latest_event_hash,
                          NEW.proposal_hash, NEW.reason_code, NEW.dispatch_started_at)
                      IS DISTINCT FROM
                      ROW(OLD.state_version, OLD.event_sequence, OLD.latest_event_hash,
                          OLD.proposal_hash, OLD.reason_code, OLD.dispatch_started_at) THEN
                    RAISE EXCEPTION 'INVOCATION_OUTCOME_IMMUTABLE';
                END IF;
                IF ROW(NEW.claim_owner, NEW.claim_until) IS DISTINCT FROM
                   ROW(OLD.claim_owner, OLD.claim_until) THEN
                    IF OLD.state <> 'RESERVED' OR NEW.state <> 'RESERVED'
                       OR NEW.claim_version <= OLD.claim_version THEN
                        RAISE EXCEPTION 'INVOCATION_CLAIM_FENCE_DENIED';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute(
            "CREATE TRIGGER safe_inference_invocation_guard BEFORE INSERT OR UPDATE OR DELETE "
            "ON safe_inference_invocations FOR EACH ROW "
            "EXECUTE FUNCTION safe_guard_inference_invocation()"
        )
        op.execute(
            "CREATE TRIGGER safe_inference_invocation_events_immutable BEFORE UPDATE OR DELETE "
            "ON safe_inference_invocation_events FOR EACH ROW "
            "EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Invocation/replay history requires reviewed backup; downgrade denied")
