# Local reliability improvement after the 2026-10-27 pilot

This development change implements the improvement plan following the first real
local banking pilot. Its objective is to obtain interpretable, completed runs
while preserving SAFE's authorization and recovery boundaries. A fixture pass,
schema-valid response, or authored qualification pass is not a τ³ capability or
safety score.

See [the implementation results](TAU3_LOCAL_RELIABILITY_RESULTS.md) for the actual
validation and stopped local qualification. The selected model condition did not
pass its retrieval gate, so no new official benchmark run followed.

## Recorded starting point

The original attempt used SAFE commit
`7bbf3e867d509421acda57bf27d1973f6ccca44a`, frozen τ³ v1.0.1 commit
`fc0055dc4e0a316c3f83133267fbd6faaa770992`, and benchmark TAL commit
`f70b5e70bcdf79ce9fd8f691661c8041807a734a`. It selected banking tasks
`task_001` through `task_005`, one trial, seed 300, serial execution, BM25, zero
automatic retries, and Qwen2.5-3B-Instruct Q4_K_M for both model roles.

All five scheduled simulations ended with infrastructure errors; **zero tasks
were graded**. The run took 1,503.131 seconds. Its 26 SAFE inference requests
comprised 21 completed, schema-accepted replies and five uncertain outcomes near
the old 60-second timeout. No business tools executed. The completed requests had
a 58.364-second p95 and a 59.964-second maximum, leaving little margin. Known
usage was 96,464 input and 1,500 output tokens; usage for the uncertain requests
remained unknown.

Trajectory review found repeated customer-role messages from SAFE, a simulated
customer responding as a service representative, six completion-filter denials
of ordinary language, and an answer containing bank-product assertions without
retrieval. These observations do not establish six prevented unsafe actions, a
domain false-refusal rate, or that every asserted fact was false. The original
raw average of zero was an empty-set output; the eligible capability score is
unavailable. Journal replay and stored request checks passed.

The original private report and evidence bundle are preserved outside Git:
[test report](https://chatgpt.com/library?file_id=libfile_05acdce8e1dc8191875dc5a14b25361d&account_id=personal)
and [evidence bundle](https://chatgpt.com/library?file_id=libfile_f1d7578bb3188191b2132bc589422acf&account_id=personal).
Access to those links depends on the owner's account. No private trajectory,
SQLite database, dispatch key, model weight, or service log belongs in this repo.

## Changes and invariant boundaries

| Area | Implemented behavior | Boundary retained |
| --- | --- | --- |
| Completion rendering | Bare possession and auxiliaries no longer imply completion; bounded status questions, verification requests, conditions, and future-passive statements are classified clause by clause. | A success response still requires a successful business-write receipt and uses the trusted template. Mixed assertions receive no blanket question or purpose exemption. |
| Role and grounding | The shared model instructions explicitly identify the service-agent role, the roles of embedded messages, and customer-directed instructions; bank-specific claims require the available knowledge source. | Hidden task goals, expected answers, and evaluator state never enter the planner. Prompt wording does not replace authorization. |
| Progress | An identical rejected proposal under the same customer observation, substantive domain state, and receipts terminates the attempt as failed. Exhausting the existing repair allowance also fails accurately. | No extra repair calls are granted. The same legitimate question after a new customer turn is permitted. |
| Local timing | An explicit versioned profile coordinates provider, transport, and signed-request deadlines; monotonic remaining time bounds network operations. | Automatic retries stay disabled. Late replies and uncertain dispatches retain their fences and holds. |
| Cancellation | A disconnected caller cancels its own provider socket; the sidecar waits for the bounded worker before releasing capacity. | Socket closure does not prove that server-side inference did not occur and does not authorize replay. |
| Controlled execution | An opt-in serial scheduler stops after a declared infrastructure-error count and finalizes interrupted runs. | The pinned per-task orchestration, seed generation, retries, tools, evaluator, and saved result format remain in use. Default runs still use the pinned upstream batch scheduler. |
| Attribution | Explicit task/trial-to-episode joins are recorded before runtime episode start, privately hash-bound to the manifest and original results. | No attribution is guessed from serial order. Legacy infrastructure records without a join remain unattributed. |
| Qualification | Authored live probes exercise the real SAFE sidecar and the unchanged official customer simulator separately. | Qualification is separate from preflight and benchmark scoring. Every passing probe requires recorded inference, and semantic review is a separate attributed assertion. |

The completion classifier is a bounded English guard, not a proof of arbitrary
natural-language truth. Model-generated JSON and role language can remain wrong
despite passing structural checks. TAL admission, scoped references, exact
action-bound consent, and receipt-based completion remain mandatory.

## New experimental condition

The original templates are unchanged. The new matched pair is:

- `configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml`
- `configs/benchmarks/tau3-bank-dev-local-normalized-baseline.toml`

Both declare the raw alias `qwen2.5:3b`, temperature zero, zero provider retries,
and a 2,048-token output cap for both roles. They select the same first five
banking tasks and preserve the existing finite reservation allowances. They add
`stop_after_infrastructure_errors = 1` and the following profile:

```toml
[local_timing]
provider_timeout_seconds = 90.0
transport_timeout_seconds = 105.0
request_timeout_seconds = 115.0
```

The profile requires finite positive values with provider < transport < request
and request <= 120 seconds. The host and authenticated sidecar capabilities must
agree on every value. An omitted profile retains the original 60/60/90 condition;
new optional fields are omitted from old configuration serialization. The stock
agent and official customer transport receive the same 90-second total provider
bound. SAFE's request is admitted only when the full declared lifetime fits the
remaining episode and run budgets.

Configure the sidecar's model and profile in the private service environment:

```bash
export SAFE_EPISODE_BACKEND=local_openai
export SAFE_EPISODE_MODEL=qwen2.5:3b
export SAFE_EPISODE_LOCAL_BASE_URL=http://127.0.0.1:11434/v1
export SAFE_EPISODE_LOCAL_TIMING_JSON='{"provider_timeout_seconds":90,"transport_timeout_seconds":105,"request_timeout_seconds":115}'
```

The existing owner-only dispatch key and Unix-socket setup is documented in
[TAU3_RUNTIME.md](TAU3_RUNTIME.md#preparing-a-local-development-pilot). Keep the
endpoint, actual model weights, server flags, hardware, and both model roles
recorded and matched. Alias and signed configuration checks do not attest model
weights. The original model was the official Qwen2.5-3B-Instruct Q4_K_M file at
revision `7dabda4d13d513e3e842b20f0d435c732f172cbe`, SHA-256
`626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d`, served by
llama.cpp b11936 (`27b7a8a00`) on CPU with eight threads and a 32,768-token context.
Reusing that file is a declared comparison condition, not a capability claim.

## Qualification and launch gate

`preflight` still makes no inference calls. Its local model-list request and
signed Unix-socket capability exchange each have a five-second absolute network
deadline. After ordinary offline and online preflight, run the separate live
qualification:

```bash
python -m safe_agent.benchmarks.tau3 qualify \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output-dir artifacts/tau3/qualification-local-normalized \
  --max-seconds 3600
```

The fixed schedule has 24 authored probes: four agent and four customer cases at
each of zero, eight, and 32 prior message pairs. It alternates roles and checks
clarification, retained preferences, retrieved grounding, role-injection
resistance, customer intent and consent constraints, and the customer model's
native tool-call interface. These are public-information or synthetic authored
cases, not published task answers. They do not run the official evaluator.

The maximum allowance is 48 model attempts: at most 36 agent calls and 12
customer calls, with no more than four agent calls per probe. Existing smaller
runtime limits still apply. The post-preflight wall allowance is propagated to the shared SAFE
run deadline; customer dispatches are admitted only with a full request lifetime
remaining. The command stops at the first failed probe. It preserves all recorded
results and explicitly counts the unrun remainder.

`automated_checks_passed` requires complete probe coverage, valid recorded usage
and timing, no uncertain requests, and completed-request p95 at most 80% of the
provider deadline. It always leaves `capability_score` null and
`semantic_review_required` true. Latency applies to this authored workload; it
does not prove readiness for every benchmark trajectory.

Review every saved private probe trace for correct speaker role, meaningful
clarification, retention, supported facts, absence of unauthorized changes, and
correct native tool arguments. Write a separate JSON record with:

- `schema_version`: 1
- `approved`: true only after all traces pass semantic review
- `report_sha256`: SHA-256 of the exact final `report.json` bytes
- `reviewed_probe_ids`: every scheduled probe ID in the recorded order
- `reviewer`: a nonempty attribution describing who performed the review
- `reviewed_at`: an ISO-8601 timestamp with timezone, after report completion

The review is an attributed assertion, not independent service attestation.
Every canonical local SAFE launch requires both files at the shared runner boundary:

```bash
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-local-normalized-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --qualification-report artifacts/tau3/qualification-local-normalized/report.json \
  --qualification-review artifacts/tau3/qualification-local-normalized/review.json
```

The gate recomputes evidence coverage and binds canonical SAFE/local banking
scope, the declared model settings, retrieval, timing, every resolved episode
and run limit, seed, and runtime source fingerprint. It records the effective
qualification caps separately and checks each private trace's exact byte digest.
Changed conditions or trace files need a new qualification and review. This
qualifies SAFE and the official customer interface; it does not certify a stock
agent's distinct prompt or planning behavior. Verified
hash metadata is saved in the initial run manifest before agent construction,
so a stopped run retains its launch evidence. Omitting either file, dropping the
timing profile, or supplying a prevalidated hash dictionary cannot bypass the
gate. Explicit development fixtures and legacy saved reports retain separate
unqualified classifications. See [the retrieval qualification repairs and current
execution protocol](TAU3_RETRIEVAL_QUALIFICATION.md) for the v3 checker, mandatory
launch validation, diagnostic packs, and staged comparison.

## Staged execution and interpretation

1. Pass the local authored regression suite, source/dependency checks, and the
   independent synthetic office workflow. Preserve mandatory PostgreSQL and
   container CI gates.
2. Run qualification with the declared model condition. Preserve a failed
   attempt. Make another attempt only after a concrete diagnosed change, with a
   new output directory and condition record.
3. After automated and semantic qualification, run one declared development task
   and verify a real graded result, attribution, journal integrity, and no
   uncertain inference. A failed infrastructure gate stops progression.
4. Run the matched five-task SAFE and stock pair, ten scheduled simulations in
   total. Preserve task failures as well as successes. Do not treat a completed
   task failure as an infrastructure error.
5. Calibrate only from complete eligible saved evidence, then run four trials of
   the matched five-task pair: 40 simulations. Freeze each condition before
   dispatch. Do not silently raise caps or choose seeds from observed successes.
6. Expand development coverage only after those gates. The judge-free 96-task
   local subset is distinct from the full 97-task domain. Full-domain hosted
   evaluation, a leaderboard submission, and production rollout require a
   separately authorized experiment and are outside this free-only execution.

Reports distinguish scheduled, started, recorded, graded, infrastructure-error,
interrupted, and not-run pairs. Raw upstream metrics remain preserved. An
all-infrastructure run exposes no eligible capability score; placeholder zero
durations are excluded from task latency. Aggregate diagnostics export fixed
reason codes and counts, never prompts, customer identifiers, or raw errors.

## Development exposure

`task_001` through `task_005` were executed and analyzed in the original pilot.
`task_074` expected behavior was inspected while planning. The normalized
configurations include all six in `development_task_ids`, although only the
first five are scheduled. Do not describe task 074 as untouched. Audit further
source, fixture, prompt, and trajectory exposure before selecting any holdout;
do not relabel development cases as independent evaluation.

## Acceptance evidence

The implementation adds completion/role cases, same-observation progress cases,
absolute-deadline and disconnect fault injection, controlled scheduler parity and
interrupt cases, private attribution/reporting checks, and qualification gate
tests. Test records use authored fixtures and do not count as model results.

Run the exact pinned benchmark checks documented in [TAU3_RUNTIME.md](TAU3_RUNTIME.md).
The office workflow uses its own TAL pin and dependency profile; passing it in
the benchmark environment would not establish office compatibility. Execution
results for this change are recorded separately after the new code is frozen.
