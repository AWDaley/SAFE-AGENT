# W4 operator recovery — staged implementation

**Status: draft PR #34, stacked on PR #36. CI and independent review are required before merge. Not production approved.**

This increment builds on W4 commit `45305a9da56c1a88917b54bd59afd0509c1b1d96`. It reuses the existing W3 durable result records and W4 atomic consumption; it adds no competing result store, planning lease system, or accounting ledger. The candidate patch has now been uploaded to GitHub as smaller reviewed commits following an earlier unsuccessful tool submission.

## Scope

Add authenticated, bounded operator diagnostics and a separate **saved-result
resume** command. The command can only attempt to consume a validated result
that is already stored; it never calls inference execution or admission, starts
another attempt, approves an action, queues an effect, or reconciles a charge.

The existing create_app wiring registers the new API only when its configured
planning service is TrustedCoordinatedPlanning. The recovery page is served only
when operator UI is also enabled. Normal assignment/approval/execution routes
are unchanged. The main workspace links to the optional recovery page, which
has its own in-memory test-token session. No credentials are passed in links.

### API

- `GET /v1/tasks/{task_id}/planning/recovery`: tenant-scoped status, original
  inference deadline, current task/workspace versions, and allowed actions.
- `POST /v1/tasks/{task_id}/planning/resume-saved`: closed command containing
  expected task/workspace versions and a recovery revision from a recent GET.
- `/operator/recovery`: an opt-in recovery console for those two operations.

GET does not mutate durable state or inference history. It can rehydrate trusted
in-memory references and projections while checking the synthetic source.
It does not advance admission clocks, release holds, or rewrite a
DISPATCH_STARTED record as UNKNOWN. Source/database reads and locks still occur.

The diagnosis and the recorded invocation state are separate. A dispatch marker
without a result becomes *observationally uncertain* after its deadline; the
recorded invocation remains unchanged. Existing UNKNOWN work remains uncertain
even when its lease expires. Expiry is not evidence of no charge or no execution.

### Operator states

| Diagnostic | Permitted recovery action |
|---|---|
| NOT_STARTED | Inspect only; normal planning remains separate |
| INPUT_MISSING / INPUT_SAVED | Inspect only; do not regenerate or dispatch from recovery |
| WAITING_TO_DISPATCH / INFERENCE_PENDING | Refresh only |
| INFERENCE_UNCERTAIN / INFERENCE_STOPPED | Refresh and obtain trusted review; no refund/replacement |
| RESULT_RECOVERABLE | Authorized initiator may prepare the saved result for review |
| RESULT_EXPIRED / RESULT_MISSING | Refresh only; no renewal or replacement inference |
| SOURCE_CHANGED / AUTHORITY_CHANGED / POLICY_CHANGED | Refresh and obtain trusted review |
| PREPARED / NEEDS_CLARIFICATION / TASK_ADVANCED | Inspect existing evidence; do not repeat preparation |
| BLOCKED | No recovery action |

A same-tenant reader may inspect but cannot resume without the original
initiator identity and task:read, task:prepare, and task:propose scopes. The
response is advisory, not a permit. The revision includes the observed state,
input identity, invocation/consumption evidence, configuration policy and
operator identity/scopes. Resume checks this revision and then invokes the
existing W4 transaction, which revalidates current authority, policy, source and
original deadlines under its own lock.

The saved candidate and the final proposal are not interchangeable. Only W4
constructs the simulated final proposal, records its consumption, and requires
approval of that final action. No outbox is created by this new command. A lost
consumption-commit acknowledgment is handled by inspecting the exact committed
consumption; it never invokes inference again.

A repeated stale command may return HTTP 409 after another caller completes
preparation. The user must explicitly refresh. Safe repetition here means no
duplicate work; it does not promise identical HTTP responses to stale commands.

## User interface protections

The recovery console exposes no execute, approve, cancel, refund or replan
button. It uses same-origin fixed routes, bounded task IDs, closed commands and
fixed explanatory copy. Response bodies are never used as HTML or error text.

The bearer token remains in page memory, is cleared from its input, and is not
stored in cookies, browser storage, URLs or generated DOM. Switching task/session
invalidates previous results and aborts the browser request. Late responses
cannot restore an action for a previously selected task. Aborting a submitted
preparation request does not undo server work; the page explicitly directs the
operator to inspect recorded state after a lost or conflicting response.

## Validation actually performed

- **16 Chromium DOM/JavaScript cases passed**, in three grouped invocations,
  using Playwright 1.57.0 and local `/usr/bin/chromium` with Python 3.13.5.
- The browser had all networking aborted; HTML and JavaScript were loaded from
  local files and fetch responses were supplied by an in-page fixture. These are
  interaction tests, not end-to-end HTTP/backend or production browser tests.
- The cases cover explicit resume-only requests, blocked states, memory-only
  sessions, task changes while requests are pending, invalid responses, conflicting
  resumes, wrong-task responses and invalid sessions.
- Node syntax checking and Python compilation passed for the candidate files.
- **15 repository API/service tests and 13 PostgreSQL cases were authored but
  NOT executed.** Ruff, mypy, the full existing suite, migration regression jobs,
  TAL workflows and repository CI were NOT run on this candidate.

The environment could not clone the repository (DNS resolution failed). Its
managed Chromium also blocked navigation to the localhost test server; no
browser policy was changed. Network-free UI tests were used instead. Initial
whole-suite local calls exceeded the execution limit; only the completed grouped
runs recorded in validation/browser-results.xml count as successful evidence.

The W4 recovery CI workflow requires real PostgreSQL and browser test
execution, with minimum-count and zero-skip assertions. Its actual GitHub Actions result determines acceptance. Existing
W3/W4/TAL checks remain required, not replaced by the new workflow.

## Required review and testing before merge

Run Ruff, mypy, the 15 API/service tests, all 13 real PostgreSQL cases, the full existing suite, and the existing W3/W4/TAL workflows on the current pull-request head. Review observational status, per-operator revision binding, GET-side database writes, authority and source changes between GET and POST, lost acknowledgments, concurrency, and packaged UI assets. Check the actual GitHub Actions runs, rather than interpreting the earlier local browser evidence as full backend verification.

## Deliberately unchanged boundaries

No schema migration or historical-migration edits. No live databases were
modified. No hosted inference, Google writes, customer credentials, production
authorization, new model tools, automatic refunds or execution permissions.
The canonical budget implementation and W4 one-input-per-task restriction remain.

Cancellation, reviewed retirement/replanning, explicit business cancellation,
complete browser-to-backend acceptance, deployable startup, process isolation,
backup rollback protection, branch rules and full build locking remain separate
work. This patch does not establish that those gates are complete, and it is not
a substitute for independent non-author review.
