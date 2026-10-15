# M7-05c — Fenced offline dispatch coordinator

Status: implementation for review, built on the merged M7-05a/b foundation.
This is not independent security sign-off. **M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.**
No hosted model, provider credential, real invoice or business write is enabled.

## Delivered path

`TrustedInferenceCoordinator.execute(principal, planning_lease, canonical_prompt)`
uses the existing atomic admission service, then a task-locked claim and a
separately committed DISPATCH_STARTED event before issuing exactly one private
Unix-socket request. It returns a validated, non-executable proposal only after
recording its digest under the original task/manifest/projection/deadline.

The coordinated path has an at-most-once dispatch policy for each admitted
planning attempt. It does not promise exactly-once successful inference. A crash
between the dispatch marker and sending may leave a request that never actually
ran in UNKNOWN. Availability is deliberately sacrificed rather than guessing
that it is safe to send a second time.

## Claims, transactions and accounting

Claims use a random internal token (only its digest is stored), a monotonic
claim version and an expiry bounded by the immutable request deadline. A claim
may be replaced after expiry only while RESERVED. Every start checks the exact
owner/version again under the task lock. Once DISPATCH_STARTED, no worker can
reclaim or resend that invocation, even after its claim or request expires.
Claims are never model inputs, browser commands or executable bearer tools.

All state changes append the existing sealed invocation events. The dispatch
marker and canonical cost UNCERTAIN event commit in the same transaction.
No network runs while SQL locks are held. A commit error prevents the caller
from sending. A response-loss error never resets a marker. A finish-commit
response loss is resolved by inspecting the recorded state, not resending.

Cost reservations remain REMOTE-bound and conservatively held even for the
local mock, including successful replies. No timeout, reported usage, model
claim of zero cost, or invocation transition can clear a financial hold. The
existing independently authenticated reconciliation path is unchanged.
Outstanding admission capacity retires only on a trusted synchronous validated
reply or on FAILED_BEFORE_DISPATCH. UNKNOWN continues to count against capacity;
lease expiry alone does not prove that a backend stopped. No general unknown
work retirement, refund, cancellation or override endpoint is added.

## Private sidecar boundary

Set `SAFE_INFER_DISPATCH_KEY_FILE` to an owner-only file containing a separate
inference-only signing secret when launching `safe_agent.inference_service`.
The existing owner-only workload token is still mandatory. These must be
DIFFERENT secrets. In this mode `/v1/infer` does not exist; only `/v1/dispatch`
accepts a strictly typed, coordinator-authenticated envelope. The signature
binds the exact request, invocation hash, dispatch event and claim version.
The model receives only the existing minimized task/free-busy request, never
these secrets, SQL, broker access, event evidence or an execution tool.

`CoordinatedUDSInferenceTransport` permits only an absolute private owner-only
Linux Unix socket, checks the connected peer UID, uses separate workload and
dispatch secrets, and sends once with no proxy, DNS, TCP, redirects or retries.
Monotonic deadlines are reapplied for socket reads, including trickling HTTP
headers, and responses have byte/framing/type limits. These controls do not
constitute a hard deadline for arbitrary model computation. The sidecar still
accepts only the exact deterministic offline mock backend.

The coordinator and sidecar currently use the SAME dedicated trusted OS
identity; an untrusted model or operator-facing app must NOT run under that
identity or possess either credential. A signed envelope proves its caller,
not a live SQL lookup at the sidecar. It must not be distributed as a retryable
capability. The sidecar's own replay cache remains process-local; durable replay
protection is supplied by the coordinator's SQL marker and exclusive signing-key
custody. The legacy standalone mock mode remains available for protocol tests,
not as an alternative deployed entrypoint. No production isolation claim is
made for co-resident untrusted Python, root compromise, leaked signing secrets,
unrestricted plugins or arbitrary clients given the coordinator's identity.

## Socket recovery

`OwnedUnixListener` keeps an exclusive lifetime lock and a fsynced owner-only
inode journal. After a crash, it removes a stale socket ONLY when its identity
matches that journal, the ownership/modes remain valid, and a connection is
refused. It rejects unknown files, symlinks, live peers, competing listeners and
changed inodes. The lock file itself is not deleted, avoiding lock-inode races.
A crash before ownership journaling can leave an unknown socket that requires
trusted inspection; there is no blanket deletion or unsafe fallback.

## Verification and scope

The focused tests cover competing claims, expired/forged claims, transaction
rollback, stale and hostile replies, clock regression, byte/usage limits,
no-refund accounting, unavailable databases, conservative UNKNOWN capacity,
signature binding, disabled raw routes, schema bytes, socket ownership and
slow-trickling headers. The dedicated PostgreSQL job runs six dispatch cases
against real migrated PostgreSQL, in addition to existing admission/budget tests.

`python scripts/smoke_inference_coordinator.py` uses disposable synthetic data,
a real sidecar process and a separate trusted caller process. The caller exits
abnormally after a real response but before recording completion. After restarting
the sidecar and reconstructing the coordinator with a new SQL connection, the
script verifies zero new sidecar admissions, then UNKNOWN with the hold retained.
The smoke imports test fixtures and runs from a repository checkout; it is an
acceptance script, not a packaged end-user CLI. A cross-UID access-denial probe
also runs when the test host is root; ordinary CI still checks private modes,
authentication and raw-route absence.

No historical migration is rewritten and no new migration is needed for this
increment. Existing 0009/0010/0011 lineage and hash guards remain unchanged.

M7-05d still needs the attempt-scoped persistent-planning adapter, sanitized
result persistence/reuse with freshness checks, and end-to-end approval/outbox
integration. Existing operator planning routes are NOT automatically wired to
this coordinator. M7-05e remains the broader system acceptance matrix. Production
identity separation, hardened OS egress, actual model-computation deadlines,
real billing verification and independent live-provider acceptance remain blocked.
