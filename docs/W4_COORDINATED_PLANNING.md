# W4 coordinated calendar planning

Implementation increment for review in PR #34, stacked on PR #36 at
`3fbc55172abbb28c79e05df3f52186a79d2cc234`. Main and the base PRs are not merged
by this work. This is not production authorization or completion of every W4
operator-experience requirement. Exact final CI evidence is recorded in the PR.

## Supported bootstrap and workflow

The new trusted `create_coordinated_planning(store, workflow, admission=...,
transport=...)` factory returns a `TrustedPersistentPlanning`-compatible service
for the existing operator `/plan` and `/planning` endpoints. It constructs the
coordinated inference service using `create_application_inference`. It has no
stateless generator or fallback planner. It requires the shared repository,
broker, virtual office and synthetic read/queue gateway.

```python
from safe_agent.orchestration.coordinated_planning import create_coordinated_planning

# All services are constructed by trusted host bootstrap, not request JSON.
planning = create_coordinated_planning(
    store, workflow, admission=admission, transport=coordinated_offline_transport,
)
app = create_app(
    coordinator, operator_tokens, clock=clock,
    task_provisioner=provisioner,
    synthetic_workflow=workflow,
    persistent_planning=planning,
)
```

This is an explicit opt-in bootstrap, not a silent switch of legacy demos or
all server deployments. Only approval-required synthetic calendar tasks are
supported in this increment. Internal-task planning, hosted inference and live
Google execution are not added. Browser commands remain closed version-only
requests; no browser-selected mode, raw model result or credential is accepted.

## Input, inference and consumption

Forward migration `0013_coordinated_planning` adds immutable original-input and
result-consumption evidence. One input is allowed per task. It captures the
original minimized context, sanitized calendar projection, original lease,
policy and private-source fingerprint before inference admission. A recovered
result is never paired with a newly timestamped observation.

The exact original projection can be reissued against unchanged source data
using its original timestamps. Current reference revocation/expiry, template
duration, source revision and independent synthetic provider content are checked
before inference and again at result consumption. The private fingerprint is
trusted-internal; it is not exposed to the model or browser.

The coordinator owns admission, cost holds and dispatch fencing. The application
never treats possession of a candidate, result digest or invocation receipt as
execution authority. Under the task lock, result consumption revalidates the
stored input/result/history and prepares the trusted simulation. The final
proposal, approval-required event, planning outcome and immutable consumption
link commit together; a failure rolls back the whole preparation transaction.
The final action differs from the candidate only by its trusted simulation
reference. Approval must bind the final proposal hash, never the candidate hash.

Human approval and queueing use the existing separate endpoints. No outbox row
is created by planning itself. The synthetic worker independently performs and
verifies its effect. Source reads remain optimistic: another source writer can
change data after a preparation check. Existing queue/worker freshness and
verification checks remain mandatory; W4 does not claim a distributed atomic
transaction with an external calendar.

## Recovery behavior and limits

The application can reopen saved input before admission, consume an available
W3 result after restart, or inspect a committed consumption after a lost commit
acknowledgment. These operations do not renew the original lease, projection or
inference deadline. A consumed result does not grant another approval or effect.

Pending, unknown, stale or missing results return the existing fixed HTTP 409
and retain their attempt; the current `/planning` metadata remains RUNNING for
unresolved work. This is deliberately conservative, but not a finished operator
experience. There is no automatic retirement, replacement attempt, refund or
legacy fallback. Even the generic store's begin/fail path cannot replace a
coordinated task once its immutable input exists. Clarification returns its
fixed reason idempotently rather than starting another call.

Follow-up work is required for explicit uncertainty/staleness UI states, reviewed
retirement and replanning, cancellation handling, and a configured deployment
entrypoint. The legacy framework's general attempt budget is not permission to
exceed this increment's one-coordinated-input-per-task restriction.

## Migration, evidence and rollout

Apply the additive forward migration before selecting this factory. No historical
migration or canonical budget-ledger implementation is rewritten. Upgrade tests
must preserve W3 result bytes, task history and held costs; existing results are
not automatically adopted or backfilled into W4 input records. PostgreSQL and
SQLite mutation-denying triggers protect input/consumption metadata. Destructive
downgrade refuses to erase evidence.

Quiesce old writers before changing bootstrap versions; do not switch an active
workspace back to a legacy planner. Old fixture planning remains for offline
regressions and demos, not as a recovery fallback. No existing live database was
migrated during this implementation.

Acceptance includes exact-action approval and synthetic worker completion,
repository/service restarts, concurrent callers, immutable SQL evidence,
consumption rollback, lost acknowledgments, late revocation and deadline expiry,
and dedicated PostgreSQL cases with a no-skips assertion. Separate subprocess
cases deliberately call `os._exit` after result validation and after consumption,
then recover and finish through approval/queue/worker in a fresh process with
inference disabled. Those subprocess cases use synthetic inputs and a controlled
clock; they are not live-model or power-failure certification. Existing W3 UDS
caller/sidecar acceptance remains a separate required regression.

Full repository checks execute in GitHub Actions, not as an independent local
rerun. Passing CI is not independent security review. Existing build-lock,
branch-protection, process isolation, restore and key-management work remains.
Database-owner/root compromise and stale-backup rollback are not solved by these
hashes or triggers. No live Google writes, customer credentials, hosted inference,
automatic refunds or production enablement are introduced. Keep the PR draft
and unmerged pending independent review.
