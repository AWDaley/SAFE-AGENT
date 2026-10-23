# Protected reference revocation and synthetic dispatch

The synthetic recovery worker commits a final dispatch admission before a new
provider write. Admission and supported reference revocation serialize on the
same durable task lock. A worker's cached reference map is not current write
authority.

## Ordering and supported revocation

A trusted revoker imports the task's protected mappings into a separate
`ReferenceBroker`, calls `revoke(handle)`, and saves the signed export with
`LedgerRepository.persist_protected_references`. That transaction updates both
the indexed `ReferenceRow.revoked` flag and its signed envelope. It cannot alter
the original resource identity, scope, permissions, or expiry while revoking.

Before a new write, the worker reads the provider source, then acquires the task
lock and the exact outbox lease generation. In the same SQL transaction it checks
the current signed references, indexed metadata, required bindings, task state,
deadline, projection, execution budgets, approval, and coordinated provenance.
Coordinated calendar tasks also require the reviewed TAL runtime. Only after all
checks succeed does the worker change `LEASED` to `DISPATCHING` and commit.

The **`DISPATCHING` commit is the ordering point**:

| Durable order | Result |
| --- | --- |
| Revocation commits before final admission | No new provider write is admitted; the action goes to review. |
| Final admission commits before revocation | That admitted attempt may finish, including a request already in flight. |
| Worker holds only a queue entry or lease | Neither state grants permission to bypass a later revocation. |

PostgreSQL uses `SELECT FOR UPDATE` on the task and outbox rows. SQLite uses
`BEGIN IMMEDIATE` before reading admission or revocation state. No provider I/O
occurs while the final admission transaction holds these locks. Provider source
comparison and native idempotency still apply to the write itself.

Revocation cannot retract an attempt whose final admission already committed.
Operators needing to stop every in-flight attempt must first quiesce workers and
reconcile their outstanding outcomes. Revocation is effective for future
admissions, including recovery attempts that would perform a new write.

## Recovery, readback and lease ownership

`DISPATCHING` records admission, not proof of an effect. It keeps the current
lease and attempt count. Once that lease expires, a recovery worker can claim it
as a new `LEASED` generation and first look up the provider-owned action key.

- If the effect exists, the worker verifies the stored request identity and
  independently reads its postcondition. This remains possible after revocation
  or expiry, using the original intent's scope and time to verify the signed
  mapping. The readback resolver does not import or reactivate that mapping for
  new dispatch.
- If no effect exists, the worker must obtain a new final admission using current
  authority. A previous `DISPATCHING` state cannot renew permission. Revoked,
  missing, inconsistent, expired, or unauthentic references fail closed.
- Every admission, result, uncertainty update and escalation checks the full
  claim, including its attempt count. Reusing the same worker ID cannot let an
  older process finalize a newer lease. A superseded process raises
  `StateConflict`; the supervisor records an infrastructure error and the current
  owner remains responsible for reconciliation. The result transaction retains
  the SQL lock through commit, including SQLite's writer lock; a claim cannot
  slip between the generation check and the receipt or escalation write.

The worker does not infer success from dispatch admission, a successful write
return, or a TAL journal entry alone. `VERIFIED` still requires independent
provider readback. TAL audit reconciliation accepts `DISPATCHING` as an admitted
pending action without recording a provider outcome.

## Schema and rollout

This change does not add a migration. The existing `0014` migration lineage
already stores outbox status in `VARCHAR(32)` without a closed enum or check
constraint. Operator APIs and the inbox display that status as text. The Google
offline worker retains its separate provider boundary and does not use this
synthetic dispatch state.

Worker binaries from before this change do not claim `DISPATCHING` rows. Do not
run mixed old and new synthetic worker versions against a shared queue. For an
upgrade, stop old workers and wait for their in-flight calls to finish before
starting updated workers. For rollback, stop new admissions, reconcile or drain
every `DISPATCHING` action with the updated worker, and stop those workers before
starting an older version. Do not manually rewrite a `DISPATCHING` status to
simulate a clean rollback: the provider outcome may still be uncertain.

## Regression coverage

`tests/m7/test_reference_dispatch.py` exercises normal coordinated planning,
separate manager approval, actual reviewed TAL, and durable synthetic providers.
It reproduces revocation after initial worker readback and after source checking,
compares both SQL lock orders, checks indexed/signed-reference disagreement and
missing rows, interrupts a process before and after provider commit, and verifies
stale claims with reused worker IDs. It also covers the internal-task worker path.

`tests/m7/test_reference_dispatch_postgres.py` runs the same cases on PostgreSQL
schemas built by the existing forward migrations, enabled only by
`SAFE_AGENT_TEST_POSTGRES_REVIEW=1` with a disposable
`SAFE_AGENT_DATABASE_URL`. Tests make no live provider calls.
