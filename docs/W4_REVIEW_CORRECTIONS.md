# W4 pre-merge corrections: R1 and R2

This increment addresses review `5548265779` on PR #34. It builds on
`678a5dab067f807177e58e70b282658dbe371cba`, retains the stacked PR base and
requires independent review. Passing CI is not independent approval or
permission to enable live providers. Exact final commit and CI evidence belong
in the PR conversation.

## Regression evidence before correction

Regression-only commit `2bd716b8d3ec84a77b7f0eb168158e668f8c87d2`, SAFE-AGENT
workflow `38917338704`, checks job `117028087810`, reported **5 failed, 680
passed, 87 skipped**. All five failures reproduced the reviewed behaviors:
cache rebinding after rejected restoration with both existing and reconstructed
services; queue acceptance after test-only cache corruption; rebinding an issued
projection; and a coordinated worker creating an effect without TAL. Lint,
static typing and the existing PostgreSQL/TAL jobs passed. Later steps of the
failing checks job were skipped, not counted as passes.

These are application/API and synthetic worker reproductions, not live-provider
exploits. The reconstructed-service regression does not itself kill a process;
the existing W4 abrupt subprocess tests remain separate required acceptance.

## R1: original source binding survives failed restoration

Calendar observation construction is now separate from publication. An issued
projection/fingerprint pair is write-once; a second publication must match both
values exactly. A lock serializes publication and supported synthetic source
replacement. Restoration builds an unpublished candidate using the original
timestamps, checks its current scoped references and durable source fingerprint,
and only then publishes it. The W4 caller checks independent provider content
and template scope first. Failed source checks leave observation caches unchanged.

`coordinated_execution_input` reconstructs the immutable original input and its
validated-result/consumption history under the existing task transaction. Both
the queue gateway and worker compare the outbox's source binding to this durable
baseline. A mutable or corrupted advisory cache cannot replace that baseline.
Missing coordinated evidence cannot be silently treated as a legacy fixture.
No new result table, migration or accounting mechanism is introduced.

## R2: new coordinated effects require the reviewed upstream TAL check

Every worker reconstructs the coordinated task provenance from SQL. For NEW
coordinated calendar effects it requires an exact `TrustedTALOfficeDispatchCheck`
and the reviewed TAL VCS installation. A missing checker, incompatible checker,
missing runtime, wrong installation revision or denying/incompatible runtime
result prevents the new effect. Instance-level replacement of the checker method
is not used as authority. Queue-only coordinated execution is required; the old
synchronous/inline fixture entrypoint cannot avoid the enforcing worker.

Legacy synthetic tasks without coordinated provenance retain their existing
fixture semantics. Provider-key lookup and independent readback of an effect that
already occurred do not renew TAL authority and continue when the original
inference/projection deadlines have passed. Accounting holds, task leases,
human-approval bindings and no-redispatch inference behavior are not relaxed.

`require_reviewed_tal_runtime` checks installed VCS metadata against the existing
reviewed commit and verifies the loaded core module paths belong to that
installation. It does not install or access the network. This is deployment
configuration validation, not protection from a hostile database owner, root,
forged installation metadata or malicious code in the trusted process.

## Tests and deployment conditions

The dedicated review-boundary workflow requires at least 21 non-skipped SQLite
cases and 17 non-skipped real PostgreSQL cases. Positive tests execute the pinned
TAL runtime. Negative tests inject failures only in test code and assert zero
new effects. Coverage includes separate local/provider source changes, restarted
services, cache corruption, missing/incompatible checkers, runtime refusal,
incorrect installation provenance, legacy-inline denial and committed-effect
readback after expiry. Existing W4 success and abrupt-process tests now explicitly
supply the real checker; their effect, approval and single-inference assertions
are retained.

Jobs that execute coordinated effects install the same TAL revision already used
by upstream interoperability CI. Dependency reconciliation is still incomplete:
the existing TAL pin declares cryptography below 46 while SAFE's direct fixture
lock selects 46.0.4; the TAL-installed CI environment follows TAL's dependency
resolution, as the pre-existing interoperability job did. This change does not
claim a complete combined deployment lock or production-compatible dependency
resolution. W0 dependency/build review remains required; neither upstream source
nor the historical SAFE lock is rewritten here.

Before using this change on persistent data, quiesce older writers/workers and
run the reviewed version with the existing current migrations. A pre-correction
worker binary cannot enforce a newly implemented check. No active or production
database is migrated by this development work. Original source snapshots may
expire; this correction does not refresh them or create replacement attempts.

Full verification runs in GitHub Actions, not an independent local full-suite
rerun. Complete CI, current-head review, repository merge rules and remaining
process/secret/restore hardening are still required. The checks are optimistic
with respect to independent source writers; they do not create a distributed
atomic transaction with an external calendar.

**Live Google acceptance remains BLOCKED. Hosted inference is disabled. No
customer credentials, automatic refunds, new model tools, merge or production
authorization is introduced.**
