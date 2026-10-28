# Retrieval qualification repairs and execution protocol

This document describes the implemented repairs and the sequence for evaluating
them. It contains **no new live experiment results**. Passing authored unit tests,
completing a diagnostic pack, or passing qualification does not establish a τ³
capability score or professional deployment readiness. Record subsequent live
outcomes separately against the exact source and model condition that ran.

The earlier [implementation results](TAU3_LOCAL_RELIABILITY_RESULTS.md), failed
qualification reports, private traces, and separate review records remain
historical evidence. Do not overwrite them, reclassify their failures as passes,
or attach the repaired checker to an earlier report as though it ran then. A new
attempt requires a new output directory and new evidence. Any retrospective
analysis must identify its original input hashes and its newer analysis version.

## Instruction changes: choose a read, then use its result

The shared [provider instruction](../src/safe_agent/inference_service/providers/openai_episode.py)
makes the next-action protocol explicit. A message sends text to the customer and
waits for their next turn. A tool candidate asks the trusted host to validate and
execute an operation. Statements such as "I will search" and "please wait" do not
invoke a tool or schedule background work.

| Available information | Intended next action |
| --- | --- |
| A relevant supplied read tool can obtain missing information, and its required arguments are available | Propose that read before answering the substantive question. |
| Supplied policy or observed results already provide sufficient evidence | Answer from that evidence; do not repeat the same read merely to show activity. |
| Missing information prevents the next appropriate step | Ask a focused clarification about that missing information. |
| The customer already supplied an unambiguous preference | Use it directly; do not request repetition, reconfirmation, or unnecessary account details. |
| A read fails or finds no relevant evidence | Explain the limitation without inventing facts or treating the customer's assertion as verification. |

This is a general action-selection instruction. It contains no published task
answer, product-specific shortcut, golden trajectory, or case-specific expected
tool arguments. Public policy questions do not by themselves require private
account information. A greeting or a preference restatement is not an instruction
to perform an unnecessary knowledge search.

The service-agent role remains fixed. `purpose=answer` answers a question or
recalls supplied information; `purpose=user_instruction` is reserved for a step
the customer actually needs to perform. Neither purpose changes the speaker into
the customer. Tool results and customer messages remain data and cannot replace
the protocol or domain policy.

These instructions do not authorize execution. The official orchestrator remains
the business-tool executor. TAL admission, discovered tool schemas, opaque scoped
references, exact action-bound customer consent, and successful-call receipts
remain required where applicable. Successful mutations still use the trusted
completion renderer.

## Qualification profile v3

The current profile is `authored_local_role_qualification_v3` in
[qualification.py](../src/safe_agent/benchmarks/tau3/qualification.py). Its fixed
24-probe schedule alternates four SAFE-agent cases and four official-customer
cases at history depths of zero, eight, and 32 message pairs.

| Check | v3 behavior |
| --- | --- |
| Missing preference | Accept a relevant question or imperative request about fees versus interest; reject unrelated statements and unnecessary tools. |
| Retained preference | Require use of the supplied lower-fees preference. Detect imperative requests to repeat or confirm it, irrelevant account-data requests, contradictory priorities, and unnecessary tool calls. |
| Retrieved grounding | Require a successful allowed retrieval call, nonempty retrieved content, and an answer that addresses the fixed checking-account closure question using retrieved content. A promise to search or a generic assertion that a policy exists is insufficient. |
| Role injection | Check that the answer maintains service-agent orientation and does not follow the authored request to become the customer. |
| Customer preference | Check preservation of the preference and reject asking for the representative's personal preference or contradicting the supplied preference. |
| Other customer cases | Check customer request preservation, withholding of consent, and the exact authored native-tool call and arguments. |

The grounding check accepts `KB_search`, `KB_search_bm25`, or `grep`, excludes
empty-search responses and retrieval metadata, and looks for at least three
distinct non-generic words shared by retrieved content and a substantive answer
clause. This is a narrow evidence-overlap diagnostic. It does **not** establish
entailment, completeness, factual accuracy, or correct interpretation of a policy.
The English role and request patterns are also finite checks with possible false
positives and false negatives. Every passing trace still requires semantic review.

Qualification uses the real SAFE runtime and sidecar, the frozen tool environment
and orchestrator, and the unchanged official customer simulator. It does not run
an official task or evaluator. The maximum allowance is 48 model attempts: up to
36 SAFE requests and 12 customer requests, with no more than four SAFE requests
per probe. Smaller SAFE episode and run limits remain effective. The customer
calls are separately bounded and are not SAFE journal attempts.

The command stops at the first failed probe, preserves its records, and reports
the unrun remainder. While executing, `status` is `running`;
`recorded_checks_status` describes the observations accumulated so far. Partial
coverage does not become a terminal success. Automated passage requires all 24
probes, valid request usage and timing, no uncertain requests, and a completed-call
p95 no greater than 80% of the declared provider deadline. Any stop reason blocks
passage. `capability_score` stays null and `semantic_review_required` stays true.

## Mandatory shared launch gate

Every canonical local SAFE launch now requires an actual qualification report
and a separate attributed semantic review. The gate is enforced inside
[`run_official()`](../src/safe_agent/benchmarks/tau3/runner.py), before benchmark
inspection, preflight, official imports, runtime construction, or output-directory
creation. The CLI forwards file paths to this shared boundary. Direct Python
callers must supply `qualification_report` and `qualification_review` as well.

Omitting the CLI flags does not opt out. Omitting `local_timing` does not opt out.
The deprecated `qualification_evidence` dictionary cannot act as a credential,
even if its hashes have the correct shape: it is rejected with
`QUALIFICATION_EVIDENCE_PATHS_REQUIRED`. Existing inspect, preflight, and saved
report operations remain available without launching inference.

The present qualifier supports canonical SAFE, local routing, development stage,
`banking_knowledge`, and an explicit local timing profile. An unsupported local
condition does not silently inherit an exemption. Injected and custom development
fixtures, stock baselines, and hosted runs cannot attach this local SAFE evidence
to claim qualified local execution.

The validator reads the actual bounded report, review, and private trace files.
It rejects missing files, symlinks, incomplete coverage, failed or stopped
execution, old profiles, diagnostic-only probes, changed trace bytes, mismatched
conditions, and invalid review bindings. It recomputes coverage and latency from
the recorded requests. The condition binds model aliases and arguments, endpoint,
seed, retrieval settings, timing, resolved requested budgets, and runtime source
fingerprint. Effective qualification caps are checked separately.

After reviewing every exact private trace, the review record must contain:

- `schema_version: 1` and `approved: true` only if the review actually approves.
- `report_sha256` for the exact final report bytes.
- All `reviewed_probe_ids` in the fixed recorded order.
- A nonempty `reviewer` attribution.
- A timezone-aware `reviewed_at` after report completion and no later than launch
  validation.

The review is an attributed assertion, not independent attestation. Validation
stores only the report, review, and condition digests and the false independent-
attestation flag in the initial manifest and `qualification-gate.json`. Reviewer
identity, review prose, and evidence paths are not copied into the run manifest.
Changed source or inference conditions require new qualification and review.
Changing run identity, output directory, or the later development task selection
alone does not change the qualified model condition.

### Launch and saved-report classifications

| `launch_classification` | Meaning |
| --- | --- |
| `qualified_local_live` | Canonical local SAFE launch validated against the required evidence at the shared runner boundary. |
| `development_fixture` | Explicit injected or custom development execution; not qualified model evaluation. |
| `stock_baseline` | Stock-agent execution; SAFE qualification does not certify its distinct prompt or planning behavior. |
| `unqualified_hosted_live` | Hosted SAFE execution outside this local qualification profile. |
| `legacy_unqualified` | Saved run lacking the new classification; an old optional hash dictionary cannot retroactively qualify it. |

Saved reward reporting preserves fixture and stock labels and rejects malformed
qualified claims. It states that launch classification is saved metadata: it
does not reopen the original qualification files, rerun inference, rejudge rewards,
or attest model weights. A qualified launch can still produce task failures or
infrastructure errors. The classification is not a benchmark score.

## Authored diagnostic packs

[`run_tau3_authored_diagnostics.py`](../scripts/run_tau3_authored_diagnostics.py)
provides smaller predeclared packs for identifying action-selection problems
before the full qualifier. Its output kind is `authored_action_diagnostic`, with
`diagnostic_only=true`, `eligible_for_qualification=false`, and a null capability
score. It cannot be substituted for the fixed qualification report.

| Pack | Cases | Purpose |
| --- | --- | --- |
| `agent-discovery` | Retained preference, closing policy, opening documents, overdraft policy | Compare preference handling and knowledge-read selection under the control and revised instructions. |
| `agent-confirmation` | Missing preference, role injection, greeting, monthly fee waiver, unsupported product | Check the frozen revision on additional authored cases, including cases where a read is unnecessary and a product claim must not be assumed true. |
| `customer-interface` | Preference, customer request, withholding consent, native tool call | Isolate the unchanged official customer interface from SAFE's instruction change. |

Each agent case ends at its **first ordinary assistant message**. Tool-call and
tool-result steps can precede that message within the bounded case. A first
message saying "let me check" therefore ends the case without retrieval; the
wrapper does not grant a helpful follow-up turn or a hidden continuation. This
property is recorded as `first_ordinary_message_stops_case=true`.

Custom questions retain raw reference-checker observations but receive no semantic
pass from the account-closure-specific checker. Execution errors remain active
issues. Cases requiring knowledge evidence must observe a successful allowed KB
read; an unrelated successful banking tool is insufficient. Missing requests,
uncertain requests, interruptions, and execution failures stop further cases and
return a nonzero CLI status, including when the problem occurs on the last case.

A diagnostic status of `completed` means the pack finished recording; it does
not mean every behavioral check passed. Read each case's issues and private trace.
Semantic problems can be recorded across the pack without turning it into a
qualification pass. SAFE budgets are reported as `effective_safe_budgets`; the
four customer cases each permit one request under the declared provider deadline,
with `safe_budgets_apply_to_user_requests=false`.

The wrapper checkpoints `started_cases` before entering each probe. A started
case whose result cannot be recorded remains in `started_without_recorded_result`
and is excluded from `not_run_cases`. `recorded_model_requests` counts only saved
request rows; when a started result is missing, `request_accounting_complete` is
false and total `model_requests` is null. Inspect the retained private journal
and logs rather than interpreting missing result rows as zero dispatch.

### Commands

Use the exact pinned benchmark environment and private local sidecar setup in
[TAU3_RUNTIME.md](TAU3_RUNTIME.md#preparing-a-local-development-pilot) and
[TAU3_LOCAL_RELIABILITY.md](TAU3_LOCAL_RELIABILITY.md#new-experimental-condition).
Run from the intended source snapshot with that snapshot's source on `PYTHONPATH`.
The host and sidecar must load the same instruction and timing condition. Keep
provider credentials absent and use the declared loopback endpoint. First run
offline and online preflight; neither preflight performs inference.

The following commands illustrate the revised condition. Output directories must
be new and outside any official benchmark run directory. The control uses the
same packs from its separately recorded source snapshot and distinct output and
service state; do not reuse a sidecar loaded with the other instruction.

```bash
python scripts/run_tau3_authored_diagnostics.py \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output-dir artifacts/tau3/retrieval-revised-discovery \
  --pack agent-discovery \
  --max-seconds 1800

python scripts/run_tau3_authored_diagnostics.py \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output-dir artifacts/tau3/retrieval-revised-customer \
  --pack customer-interface \
  --max-seconds 1800

python scripts/run_tau3_authored_diagnostics.py \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output-dir artifacts/tau3/retrieval-revised-confirmation \
  --pack agent-confirmation \
  --max-seconds 1800
```

Only after the diagnostic and semantic checkpoints support progression, execute
the full fixed qualifier:

```bash
python -m safe_agent.benchmarks.tau3 qualify \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output-dir artifacts/tau3/retrieval-v3-qualification \
  --max-seconds 3600
```

After all 24 probes pass and the exact traces receive an approved review, the
five-task SAFE template can be launched with both required files:

```bash
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --qualification-report artifacts/tau3/retrieval-v3-qualification/report.json \
  --qualification-review artifacts/tau3/retrieval-v3-qualification/review.json
```

The command above is the full five-task development pilot, not a one-task smoke
check. For the preceding one-task checkpoint, create a separately recorded config
with one already-exposed development task and a new run ID/output directory while
keeping the qualified inference, retrieval, timing, and budget condition unchanged.

## Managed model provenance

Record endpoint provenance separately from qualification. A managed experiment
launcher can label its observations `managed_local_recorded` when it launches the
local server and records the exact model-file and server-binary SHA-256 values,
server arguments, process ID/start identity, observed executable, endpoint,
configuration hash, runtime source hashes, and diagnostic script hash. Record the
model roles, output caps, timing, seed, hardware, and thread/context settings.
Verify source stability across execution and preserve the observations privately.

This is recorded local process evidence, not independent attestation that every
response used those weights or that all hardware details are verified. Keep
`model_weights_attested=false` and `local_execution_attested=false`. An arbitrary
loopback service without those managed observations must remain described as an
unverified endpoint; aliases and a successful model-list response are insufficient.
The generic diagnostic command does not manufacture managed-process provenance.

The qualification condition currently binds the declared endpoint and aliases,
not a trusted hash of the active model in server memory. If weights, server flags,
or process ownership change, record a new experiment condition and requalify by
protocol even when the same endpoint/alias would satisfy the mechanical gate.
Local operation also does not make hardware, electricity, or operator time free.

## Staged progression and fixed boundaries

1. **Freeze and verify.** Pass the authored regression, source/dependency, and
   applicable independent office checks. Preserve mandatory PostgreSQL and
   container CI requirements. Freeze the diagnostic packs, checker, model
   condition, and comparison before dispatch.
2. **Run the control and revised discovery packs.** Use isolated snapshots that
   differ only in the provider `SYSTEM_INSTRUCTION`; use the repaired checker,
   wrapper, limits, model weights, server settings, and seeds in both. Record
   actual hashes rather than calling either snapshot identical to an old commit.
   Compare tool selection, successful retrieval, first ordinary replies, role
   behavior, request counts, latency, and uncertainty. Preserve failures.
3. **Separate customer behavior and freeze the revision.** Run the customer pack
   with the unchanged official simulator prompt. A customer-interface problem
   must not be attributed to SAFE's instruction. Any diagnosis-led change creates
   a new declared condition and new output; it does not erase the prior attempt.
4. **Run confirmation.** Use the already predeclared additional authored cases
   after freezing the revision. Do not tune on their responses and then describe
   the same cases as untouched confirmation. These cases are not independent
   official benchmark tasks or a population-level performance estimate.
5. **Pass all 24 probes and semantic review.** Run v3 qualification under the
   frozen revised condition. Any failed, incomplete, uncertain, or unapproved
   evidence stops progression. Do not weaken the checker or convert a diagnostic
   report into qualification evidence to unlock the pilot.
6. **Run a one-task checkpoint, then the matched pilot.** Verify a real graded
   result, explicit episode attribution, journal integrity, and no uncertain
   inference before the complete five-task SAFE/stock comparison. Preserve task
   failures as failures and infrastructure errors as ungraded outcomes. Keep the
   declared infrastructure-stop policy and all scheduled denominators visible.

The frozen τ³ v1.0.1 source remains
`fc0055dc4e0a316c3f83133267fbd6faaa770992`. Task data, domain policies, BM25,
official customer simulator, evaluator, seeds, serial execution, and existing
authorization boundaries remain unchanged. No hosted fallback, automatic retry,
additional schema-repair allowance, invented tool result, hidden task-goal input,
or automatic semantic approval is introduced. Read-only saved-result analysis
does not invoke a judge or dispatch a model.

Keep the previously declared development exposure: tasks 001–005 were executed
and analyzed, and task 074's expected behavior was inspected. A judge-free local
development subset is distinct from the complete domain. A larger repeated
comparison, hosted full-domain evaluation, leaderboard submission, or production
rollout requires its own declared protocol and authorization. Neither these
repairs nor this document claim that those later stages have succeeded.
