# M6-01b — Trusted synthetic task assignment

**Status:** Implemented as opt-in local synthetic task intake in PR #20.
M5 live Google acceptance remains BLOCKED. Assignment never calls a planner,
dispatches a provider write, or grants browser/model execution authority.

## Trusted catalog and API

A privileged installation supplies TrustedTaskProvisioner with a SyntheticOfficeState,
a shared ReferenceBroker, LedgerRepository and TrustedOfficeTasks coordinator,
and a tuple of TrustedSyntheticOption records. These private options explicitly
bind one organization and one initiating actor to allowed synthetic source
resources and a workflow. The constructor validates those private bindings
against the loaded synthetic fixture, including template duration and time zone.

An operator with task:create scope may GET /v1/task-options. The response
contains only selected option identifiers, workflow codes and bounded scheduling
policy. It never exposes customer/calendar IDs or the private option catalog.

POST /v1/tasks accepts a versioned closed payload with option_id, a random
req_ prefixed 128-bit hexadecimal idempotency key, and allowed_start/allowed_end
as timezone-aware ISO 8601 timestamps. The operator cannot submit an organization
ID, actor ID, source resource ID, TAL manifest, permission, grant, external
connection or custom budget. Unknown properties fail schema validation.

The trusted provisioner generates a new task ID and manifest ID, immutable
hash-bound manifest, one authorized synthetic operation, bounded effects/cycles,
a fixed 24-hour action deadline, and appropriate actor/tenant/task/operation/
expiry-bound opaque references. It returns a task receipt containing only
task ID, phase, state version and explicit synthetic-only/no-provider-write
flags. All new tasks begin at ASSIGNED. A later trusted preparation action
must generate and persist a proposal before one can be reviewed.

## Atomic records and idempotency

Alembic revision 0007_trusted_task_intake adds safe_task_intake_keys.
The tenant/actor/idempotency-key tuple uniquely identifies one request digest
and task. The initial task, immutable manifest, hash-linked TASK_CREATED
event, protected broker reference records and intake key are committed
in one SQL transaction. Missing or inconsistent protected references deny
creation before SQL writes. A failed database commit rolls all records back.

Replaying the same key and request returns the existing task without granting
a new budget or extending the deadline. Reusing a key for changed parameters
returns HTTP 409. Concurrent writers race against the unique SQL constraint;
a losing request may return only an existing exact-digest task.

The broker's HMAC-protected mappings never appear in an HTTP response, model
context, or regular logs. Subsequent process restarts must re-import the
private mappings under the same protected broker signing key. Preserving the
broker key securely across restarts is required for any durable deployment.
Unused in-memory handles from a failed registration are not authorizations.

## Opt-in server configuration

Construct the provisioner in trusted initialization code and inject it into
create_app using its task_provisioner keyword parameter. Example private
synthetic option parameters are:

    TrustedSyntheticOption(
        option_id="opt_followup",
        organization_id="org_demo",
        initiator_id="employee_demo",
        workflow="schedule_follow_up",
        customer_resource_id="cust_c1",
        calendar_resource_id="cal_main_private",
        appointment_template_id="follow_up_v1",
        time_zone="America/New_York",
    )

Use only the included synthetic fixture resources, not customer or personal
accounts. Apply alembic upgrade head to an isolated local database. Connect
to /operator on trusted localhost with a separately issued local operator
test token. The assignment form is displayed only if the configured catalog
has a matching option and the operator possesses task:create.

For a review-only deployment, omit task_provisioner. The existing inbox,
preview, approvals and evidence APIs continue to work and no task-creation
endpoint is registered.

## Validation

Run pytest -q tests/m6, the full pytest suite, Ruff and mypy, and the
isolated Alembic smoke test in GitHub Actions. The new tests cover strict
schema and bounded windows, organization/actor isolation, private reference
scope, internal task versus calendar separation, SQL rollback, idempotency
replay and zero external effects.

This milestone does not implement model planning, automatic execution,
production authentication, tenant provisioning, real application integration,
or Google-hosted acceptance. Completing the operator-driven task workflow is
a separate next milestone.
