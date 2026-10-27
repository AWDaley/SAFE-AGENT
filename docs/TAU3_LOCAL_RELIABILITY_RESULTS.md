# Local reliability implementation results — 2026-10-27

## Outcome

The improvement is implemented and committed locally. The new live qualification
correctly stopped the selected Qwen2.5-3B model condition on a retrieval failure,
before any new official benchmark run. There is **no new τ³ capability score**.
The implementation and execution protocol are in
[TAU3_LOCAL_RELIABILITY.md](TAU3_LOCAL_RELIABILITY.md).

The local branch is `improvement/tau3-local-reliability`, based on
`7bbf3e867d509421acda57bf27d1973f6ccca44a`, the head of the existing draft
[PR #45](https://github.com/AWDaley/SAFE-AGENT/pull/45) when work began.
The exact code revision used for the second live qualification is
`b918d245d18b1c49e1d6e4f758fcf62022b10fa4`; its runtime source fingerprint is
`2892d3d73599637009d2999c7d3220fee92c48f7616cff8b9233e9d32c784009`.

## Reviewable changes

| Commit | Change |
| --- | --- |
| `45113ec` | Explicit service-agent role and grounding instructions; narrower completion classification, including mixed-clause and affirmative-tail cases; receipt-bound success preserved. |
| `1e77d65` | Versioned local deadlines, total network bounds, request-scoped cancellation, conservative uncertainty, full-lifetime admission, and bounded identical-repair termination. |
| `cc018e8` | Opt-in serial infrastructure stopping, explicit private task/trial/episode joins, interruption recovery, sanitized diagnostics, and honest missing/graded score accounting. |
| `aea053b` | Bounded live role qualification, exact condition and trace bindings, separate semantic review, pre-run launch evidence, new normalized configurations, and the runbook. |
| `b918d24` | A narrowly diagnosed qualification-check correction: accept imperative clarification without requiring `?`; correctly handle typographic apostrophes and whole-word refusal language. |

The frozen τ source, task data, policies, BM25 implementation, official simulator,
and evaluator were not edited. Default benchmark runs still use the pinned
upstream batch scheduler. The controlled scheduler requires the explicit
development-only setting. Hosted APIs and live Google integrations were not used.

## Verification

| Verification | Result | Scope |
| --- | --- | --- |
| Full benchmark suite | 739 passed; zero failures, errors, or skips | Episodes, domains, and τ integration; real PostgreSQL file explicitly excluded. |
| Follow-up after final readiness timeout fix | 169 passed; zero failures, errors, or skips | Affected local transport, model boundary, container source profile, preflight, qualification, and real authored orchestrator paths. |
| Final qualification-checker cases | 55 passed; zero failures, errors, or skips | Includes the imperative and refusal-language regression cases and real BM25/orchestrator fixtures. |
| Distinct benchmark test identities across those runs | 747 | Deduplicated using JUnit class/name pairs; the overlapping suites must not be added together. |
| Ruff | Passed | Full source, tests, scripts, container probe, and benchmark migrations. |
| Mypy | Passed | All 175 source files. |
| Dependency and source provenance | Passed | τ commit, reviewed TAL pin, complete frozen inventories, generated domain catalog. |
| Office/workspace regression suites | 288 passed; zero failures, errors, or skips | Separate exact office dependency environment. |
| Installed office process acceptance | 2 passed; zero failures, errors, or skips | Real API restart, UDS, independent worker, durable recovery, and upgrade/rollback. |
| Real failed-qualification launch gate | Passed | CLI returned `QUALIFICATION_NOT_PASSED`, exit 2, before creating a benchmark output directory. |

The office environment used CPython 3.12.14, 43 hash-locked third-party packages,
and TAL `e9d09ef47c0e0e18f3955f9c58f7a8941baf6c38`. Its installed source snapshot
was taken at 22:53:37 UTC, with runtime fingerprint
`565e831f52f176dddf8c5a06ef0cff95953175c59b28e56eef387a2830e204d4`.
It predates later benchmark-only integration and renderer fixes; the office
results are attributed to that snapshot, not mislabelled as final-revision
installed-process results. The unchanged office tests, package provenance,
snapshot manifest, and earlier failed installation attempts are preserved.
The installed doctor, eight synthetic demos, M5 offline release gate, and M7
budget-lineage check also passed.

Real PostgreSQL and Docker isolation acceptance were not available locally.
Their mandatory CI workflows remain unchanged. No remote CI run for this new
branch is claimed because publication was blocked.

## Real local qualification

Both attempts used the already downloaded Qwen2.5-3B-Instruct Q4_K_M file,
served through llama.cpp b11936 on eight CPU threads, one slot, and a
32,768-token context. The model file's SHA-256 was rechecked as
`626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d`.
The local route had no hosted fallback. Provider/transport/signed-request limits
were 90/105/115 seconds; both roles declared zero retries and 2,048 output tokens.
Aliases and configuration signatures are not independent model attestation.

### Attempt 1: diagnosed checker false negative

The v1 qualification at clean revision `aea053b` stopped after its first probe.
SAFE correctly requested the missing fee-versus-interest preference using
“Please let me know which matters more…”. The classifier required a question
mark and therefore rejected an appropriate imperative request.

The exact failed report and trace remain unchanged. A separate diagnosis records
the defect. Independent review agreed with that interpretation. Version 2 added
positive and negative regression cases, and one diagnostic rerun was made in a
new output directory. The old failed execution was not promoted to a pass.

### Attempt 2: genuine retrieval failure

| Measure | Recorded result |
| --- | --- |
| Profile | `authored_local_role_qualification_v2` |
| Recorded / scheduled probes | 5 / 24 |
| Automated probe passes | 4 |
| First failed probe | `agent_grounding_history0` |
| Failure reason | `RETRIEVAL_EVIDENCE_MISSING` |
| Remaining probes not run | 19 |
| Model requests | 5 complete, zero uncertain |
| Tool calls and successful retrievals | 0 |
| Wall time | 156.418 seconds |
| Completed-request p95 and maximum | 53.816 seconds |
| Known input / output tokens | 13,658 / 203 |
| Official evaluator calls | 0 |
| Capability score | Unavailable |

The failed response offered generic process commentary and recommended checking
the knowledge base. SAFE did not invoke its knowledge tool or provide the
requested account-closing requirements. This is a retrieval and request-
fulfillment failure. The trace does not demonstrate fabricated specific
requirements, an unauthorized action, or a timeout.

An independent assistant review checked all five exact trace hashes. It agreed
with the grounding failure. It also noted that the customer preference reply
unnecessarily asked about the representative's preference, and the agent's
correct preference recall unnecessarily requested account details. Thus, four
automated passes should not be described as four unqualified semantic successes.
The separate review checkpoint has `approved: false`; no approved review was
created.

The explicit consent-withholding, adversarial role-injection, native customer
tool-call, and longer-history probes were not reached. The five completed short
cases provide no evidence that those capabilities or long-context latency pass.
Although this subset met the latency-margin calculation, it did not qualify the
model condition.

Across both attempts, six local model requests completed, with 17,958 known
input tokens and 237 output tokens. These counts are provider-reported usage,
not independently reconciled bills. No hosted API request was made. Hardware,
electricity, and elapsed development time remain separate costs.

## Durable evidence and cleanup

Read-only diagnostics verified one episode and one SAFE model attempt for v1,
and three episodes and three SAFE model attempts for v2, with no journal replay
failures. The other two v2 requests were direct calls from the unchanged official
customer simulator; they are not miscounted as SAFE ledger attempts.

The real launch command was then supplied the failed v2 report and its unapproved
semantic review. It returned `QUALIFICATION_NOT_PASSED` before creating the
pilot directory. No one-task pilot, five-task pair, four-trial comparison,
calibration from incomplete evidence, or leaderboard submission followed.

The local sidecar and model server were stopped. Exact reports, private authored
traces, read-only diagnostics, run configurations, usage, model provenance,
validation records, and cleanup evidence are retained separately from Git.
Private dispatch keys and model weights are excluded from the evidence archive.

## Remaining work and publication

The next empirical step is a separately declared local model/prompt condition
that performs the required retrieval and follows the customer's request without
unnecessary data collection. It must pass the full 24-probe schedule and semantic
review before the staged official runs resume. The frozen benchmark and current
failed evidence remain unchanged.

Automatic approval review rejected the attempted GitHub push. Its stated reason
was that the short instruction “Implement” did not establish explicit permission
to transmit this implementation to the external repository. No remote branch,
PR, merge, or deployment was created by this implementation task.

The concrete pending action is to push `improvement/tau3-local-reliability` to
`AWDaley/SAFE-AGENT` and open a draft PR targeting `feature/tau3-text-runtime`.
That would publish the prepared source, tests, and documentation for review and
allow the retained CI gates to run. It requires the user's explicit approval
under the automatic review decision.
