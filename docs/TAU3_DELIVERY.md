# τ³ implementation handoff

This change implements a runnable, isolated benchmark profile and its offline
acceptance gates. The first real local pilot on 2026-10-27 attempted five banking
tasks and recorded five infrastructure errors, with no graded tasks or eligible
capability score. The evidence and subsequent implementation are documented in
[the local reliability improvement](TAU3_LOCAL_RELIABILITY.md). The original
plan includes empirical milestones that require configured model access and
measured usage; passing authored fixtures does not complete those milestones.

Review the implementation in [SAFE PR 45](https://github.com/AWDaley/SAFE-AGENT/pull/45)
and its separately pinned admission dependency in
[TAL PR 6](https://github.com/AWDaley/TervoroAgenticLanguage-TAL-/pull/6).
The latest commit's check results are the authoritative CI status. Neither PR
is a production approval or an automatic merge request.

## Status against the original work packages

The PR labels in the original plan are work-package names, not GitHub PR numbers.

| Package | Delivered code and offline evidence | Remaining empirical acceptance |
| --- | --- | --- |
| PR 0: frozen experiment | Exact τ source/data manifest, public operation catalog, isolated dependency lock, explicit paired pilot selections, no-inference launch preflight, schedule inspection and reservation headroom disclosure | Calibrate limits from the declared pilot before final evaluation. |
| PR 1: durable episodes | Additive migrations, immutable lineage, serial calls, exact result bindings, shared run limits, SQLite and real PostgreSQL concurrency/recovery acceptance | Observe behavior under real inference latency and failures. |
| PR 2: domain admission | Separate reviewed TAL API, retail/banking schemas, customer/resource scope, encoded preconditions, exact authorization bindings | Measure policy outcomes; the encoded rules are a declared subset of the full natural-language policy. |
| PR 3: model route | Signed, bounded Unix-socket protocol; authenticated no-inference capability exchange; durable provider-request fence; fixed-origin OpenAI and explicit loopback development backends; malformed-output, timeout, and privacy fixtures | Exercise the selected route with an actual configured model; record usage, local hardware/server conditions, and applicable billing. |
| PR 4: conversation | Field-aware private references, source-bound identity inputs, one-use action confirmation, receipt-backed completion, clarification and user guidance | Evaluate ordinary conversational correctness and policy-specific wording. |
| PR 5: official loop | Actual SAFE runtime registered in the pinned official harness; sole upstream tool execution; privileged task metadata excluded | Run real model/user/evaluator trajectories. |
| PR 6: retail | Pinned operation inventory and encoded constraints; authored runtime/harness and consent/scope cases | Five-task model pilot and complete 114-task coverage report. |
| PR 7: banking retrieval | Real BM25 search, observed discovery/unlock, nested schema checking, bounded source spans/provenance, retained unlocked definitions and pending context | Measure retrieval and long-dialogue task success with the model. |
| PR 8: banking workflows | Customer-sourced two-field verification, observed resource ownership, 14 representative native workflow fixtures, scoped user-tool grants, and agent/user action separation | Five-task model pilot and complete 97-task coverage report; review semantic policy failures. |
| PR 9: recovery and isolation | No resend after uncertain effects, retained budget holds, duplicate-result fencing, private host files, and a separately constrained model-container acceptance profile | Reconcile actual provider costs and assess the chosen live deployment condition. |
| PR 10: comparison | Frozen SAFE/stock configurations, saved-pilot reservation calibration, official recorded-reward metrics, conservative schedule accounting, paired task uncertainty intervals, reviewed failure/billing records, aggregate diagnostics, and private submission validation | Run 97 tasks × 4 trials × 2 systems; produce the research comparison and reviewed submission package from those real results. |

## How to interpret the evidence

The offline suite uses authored records and scripted model replies, including
actual pinned business-tool implementations. It tests protocol, mechanical
authorization, privacy projection, consent, and recovery behavior. The dedicated
benchmark CI rejects skipped offline and PostgreSQL tests. Existing office
workflows retain their own reviewed runtime and independent acceptance gates.

The container profile has its own mandatory attempted-read probe. Its assurance
applies to that explicit image, launcher, mount, environment, and privilege
configuration. The bare Python sidecar is a trusted-host mode. A process boundary
alone does not deny same-user filesystem access, and an offline container probe
does not certify live network egress or arbitrary local model code.

No benchmark reward, leaderboard ranking, cost advantage, or competitor win can
be inferred from these tests. The same-model stock comparison measures
differences between complete systems, including prompts, planning, tool
projection, repair behavior and SAFE's limits. It does not isolate the causal
effect of enforcement. Other named competitor products require separate adapters
and recorded comparisons.

## Next execution sequence

1. Reproduce the free environment and acceptance checks in
   [the runtime runbook](TAU3_RUNTIME.md), then run offline preflight for the
   explicit retail/banking pairs in [the pilot workflow](TAU3_PILOTS.md).
2. Choose hosted OpenAI or the local banking pilot in the runtime runbook.
   Hosted runs need user/judge provider access; the local pair uses local weights
   for both model roles and tasks with no NL assertions. Keep secrets outside
   source and trajectory directories. Set `LITELLM_MODE=PRODUCTION` and run
   preflight without inference. Local readiness also checks a bounded model list;
   neither route's readiness proves inference quality or actual execution location.
3. For the local improvement condition, pass the separate authored live
   qualification and attributed semantic review first. Then run the declared
   small pilots, retain their task IDs as development exposure,
   inspect actual failures, and use `calibrate` on the real saved SAFE evidence.
   Review its cap-increases-only proposal before changing finite limits; missing
   or censored evidence cannot support a proposal. No template records a completed run.
4. Run complete coverage schedules, preserving all failures, uncertainty,
   infrastructure errors, missing results, and known/unknown cost components.
5. Freeze the final condition and run both four-trial banking configurations.
   Regenerate reports from saved rewards and judge outputs. Review false refusals
   and policy errors before making comparative safety or utility claims.

The example dollar values are conservative reservation allowances. They are not
expected prices or a cap on agent, user, judge, and infrastructure invoices
combined. `inspect` makes their model-attempt headroom visible; the checked-in
full-domain allowances may be too small to finish every conversation.
Canonical `run` requires fresh preflight before artifacts or official execution.
The wrapper and SAFE backend use zero retries at their respective layers;
hosted upstream stock/user/judge provider retries default to three when absent or
null. The local route fixes stock/user provider retries at zero and rejects any
selection needing the hosted judge. Local execution can avoid API fees, but
hardware, power, and time remain costs. Coverage/final runs still use the hosted
route. Local results cannot calibrate a different hosted condition.
Readiness and retry settings do not establish actual provider usage or costs.

See [the evaluation protocol](TAU3_EVALUATION_PROTOCOL.md) for exact metrics,
development exposure, cost accounting, and diagnostics review rules, and
[the domain coverage document](benchmark-domains.md) for the policy subset.
The [saved-result analysis guide](TAU3_REPORTS.md) documents the comparison,
billing reconciliation, and private submission commands. No live report or
submission package is generated until real complete run records are supplied.
