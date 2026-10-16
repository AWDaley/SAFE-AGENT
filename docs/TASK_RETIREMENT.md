# Coordinated task retirement

Status: offline development workflow only. This does not authorize production use,
hosted inference, or live provider writes.

## Purpose

A coordinated task can become unnecessary, stale, blocked, or unsuitable for
continued planning. SAFE-AGENT now lets the initiating operator terminally retire
eligible pre-effect work while preserving the audit trail. Retirement is not
deletion, rollback, billing reconciliation, approval, or replacement planning.

A replacement is always a newly assigned task with new authority and a new intake
identity. The retired task is never reopened.

## Allowed boundary

Retirement is offered only when there is no durable business-effect intent. It can
cover an untouched assignment, saved planning input, a request that never began
dispatch, an uncertain inference after its original deadline, a saved validated
result, a proposal awaiting approval, a ready-but-not-queued proposal, or a
rejected/clarification planning outcome.

The action is refused while an inference dispatch is still inside its original
deadline. Once an action has been queued (EXECUTING), escalated for effect review
(NEEDS_REVIEW), or verified (COMPLETED), task retirement is not an available
recovery operation. Those states require effect/reconciliation handling.

## Inference and accounting semantics

If an inference invocation is still RESERVED, retirement first transitions that
exact invocation to FAILED_BEFORE_DISPATCH. This proves only that SAFE-AGENT's
coordinated dispatch marker was never recorded. It does not manufacture billing
evidence. The existing remote-bound reservation remains conservative.

If a dispatch marker exists, retirement is unavailable until the original inference
deadline. After that deadline, the existing recovery transition records the call as
UNKNOWN before the task can retire. Its cost hold remains intact until the separate
trusted billing-reconciliation mechanism has sufficient evidence.

Validated results remain stored. Retiring a task does not delete their content,
their hashes, the immutable coordinated input, planning attempts, approvals, or
ledger events.

## Concurrency and authorization

Retirement uses the normal task-row lock and compare-and-set values from the
operator's latest recovery view:

- task state version,
- planning workspace version,
- recovery revision,
- initiating operator identity and scopes.

A concurrent approval, preparation, or queue operation that advances state wins;
the stale retirement command fails and the operator must refresh. A stale dispatch
claim also cannot start after the task reaches RETIRED, because inference dispatch
revalidates the authoritative task/attempt state.

Retirement reasons are a closed code set:

- NO_LONGER_NEEDED
- REPLACE_WITH_NEW_TASK
- STALE_OR_BLOCKED

Free-form retirement text is not stored in the event ledger.

## Operator flow

Use /operator/recovery to inspect a coordinated task. The server, not the browser,
decides whether retire_task is available. When shown, choose a closed reason and
confirm the terminal action. The response should become TASK_RETIRED with task
phase RETIRED and only the read-only refresh action remaining.

The retirement endpoint never starts inference, queues an effect, issues approval,
creates replacement work, releases an accounting hold, or contacts a provider.

## Acceptance requirements

The dedicated recovery workflow must demonstrate:

- a never-started task becomes terminal and a replacement requires a new task;
- a RESERVED invocation cannot later use its stale claim after retirement;
- DISPATCH_STARTED cannot retire before its deadline;
- expired started work becomes UNKNOWN and retains held capacity;
- validated and prepared results can retire without being queued;
- queued execution intent cannot retire;
- stale revisions and non-initiating readers cannot retire;
- PostgreSQL runs the same critical cases;
- browser controls expose retirement only from server-advertised states;
- all existing W3/W4, TAL, runtime, launcher, migration and no-live-provider gates stay green.
