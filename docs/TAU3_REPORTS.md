# Saved-result comparison and private submission preparation

The analysis commands operate on saved evaluation records. They do not make
model calls, invoke a new judge, change trajectories, or publish results. Use
them only after the run commands in [TAU3_RUNTIME.md](TAU3_RUNTIME.md) have
produced real model-backed results. Authored tests of these commands are not
benchmark scores.

## Calibrate reservation allowances from a saved pilot

Use the explicit paired selections and preflight gate in
[TAU3_PILOTS.md](TAU3_PILOTS.md) before executing a pilot. Once real canonical
SAFE results exist, inspect proposed allowances for the next declared schedule:

```bash
python -m safe_agent.benchmarks.tau3 calibrate \
  --run-dir artifacts/tau3/bank-dev-safe \
  --target-config configs/benchmarks/tau3-v1.0.1.toml \
  --headroom-factor 1.5 \
  --output artifacts/tau3-readiness/banking-calibration.json
```

The command checks recorded source/runtime/model conditions, saved-artifact
bindings, the complete task/episode join, and a read-only private-ledger snapshot.
Complete natural task failures remain in the usage sample. Missing, ungraded,
uncertain, capped, or scripted evidence withholds suggestions; authored fixtures
cannot supply the missing empirical pilot.

For each resource, eligible evidence takes the largest complete episode's held
usage, applies the explicit multiplier, then scales by the target schedule for
run allowances. `status=review_required` contains only proposed cap increases.
Existing caps are never lowered. Deadlines, prompt/output limits, repair limits,
and the fixed per-attempt hold remain unchanged. No configuration is edited and
no spend is authorized. Held token/cost units are not provider invoices; the
multiplier is not a confidence bound or a completion guarantee. The output must
be new and outside both the pilot and target run directories.

## Compare SAFE with the stock agent

```bash
python -m safe_agent.benchmarks.tau3 compare \
  --safe-run-dir artifacts/tau3/bank-final-safe \
  --baseline-run-dir artifacts/tau3/bank-final-baseline \
  --bootstrap-samples 2000 --seed 300 \
  --output artifacts/tau3/banking-comparison.json
```

Use the actual output directories recorded by your final configurations. The
optional output file must be new and outside both input runs. It is created
with owner-only permissions. Without `--output`, JSON is printed to stdout.
The bootstrap sample limit is 100–10,000; the default is 2,000.

The comparison validates the benchmark source and task inventory, scheduled
task/trial matrix, agent/user/judge models and settings, retrieval condition,
evaluator flags, and seeds before calculating paired differences. Differences
in SAFE-specific runtime limits are disclosed. The stock baseline keeps its
own agent implementation. A comparison against a separately named commercial
agent needs that agent's adapter and its own recorded experiment.

The conservative operational view retains every scheduled pair: missing,
ungraded, and infrastructure-failed outcomes count as failures. Duplicate or
unexpected result pairs are rejected before comparison; the individual run
report still exposes them for investigation. Recorded
official metrics remain separate and retain their own eligibility rules.
Task-level pass^1 and pass^4 differences use paired task-cluster bootstrap
resampling with a fixed seed and 95% percentile intervals. The task is the
resampling unit; its trials remain together. These intervals describe variation
across this benchmark's tasks. They do not prove deployment performance, remove
development exposure, or make different adaptive user conversations identical.

The report includes per-task outcomes, latency and call counts, recorded costs,
and missingness. Unknown invoice components are not replaced with reservation
holds. New completed runs bind `results.json` and `official_metrics.json` byte
hashes into their manifest. These hashes detect inconsistent post-run edits;
they do not independently attest the origin of the run.

Call counts distinguish observed assistant messages, emitted agent tool calls,
attributed tool results, and SAFE's recorded model-attempt usage. User-only tools
are excluded from agent-call counts. Missing usage remains visible; these
trajectory observations do not establish every actual provider call or retry.
The simulation wrapper uses `max_retries=0` and SAFE's provider backend uses zero
automatic retries. The pinned stock agent, user simulator, and NL judge instead
default to provider `num_retries=3` when absent or null. Those configuration
values must not be reported as observed request counts.

## Reviewed failure categories

Automatic task failure is an outcome, not a diagnosis. An optional
`--failure-annotations` JSON file can attach one reviewed primary category to a
recorded failed pair:

```json
{
  "schema_version": 1,
  "annotations": [
    {
      "run_id": "your-recorded-run-id",
      "task_id": "your-recorded-task-id",
      "trial": 0,
      "simulation_sha256": "replace-with-the-canonical-simulation-hash",
      "label": "false_refusal",
      "reviewer": "your-reviewer-reference"
    }
  ]
}
```

The evidence hash is the SHA-256 of canonical JSON for that exact saved
simulation, using `inventory.content_hash`. The command rejects stale hashes,
unknown pairs, conflicting labels, and annotations of successful outcomes.
Permitted primary labels are `reasoning_error`, `retrieval_error`,
`tool_argument_error`, `policy_violation`, `false_refusal`,
`infrastructure_failure`, `budget_exhaustion`, `uncertain_outcome`, and
`other_reviewed`. The report contains aggregate labels and evidence bindings;
it does not emit the free-text reviewer reference.

This reviewed task taxonomy is separate from the runtime's proposal-level
denial annotations used by `diagnostics`. A task can succeed after an unnecessary
refusal; such a proposal belongs in the diagnostics review even though the
whole-task failure taxonomy does not label successful tasks.

## Reconcile agent-provider billing

The official harness can aggregate an absent SAFE message cost into zero.
An optional `additional_costs.jsonl` record can replace that upstream agent
aggregate with a reviewed full-run provider amount. It cannot add the amount
on top of the same upstream component.

The reconciliation record requires:

| Field | Required meaning |
| --- | --- |
| `category` | `agent_provider_billing` |
| `run_id` | Exact recorded run ID |
| `component` | `agent` |
| `currency` | `USD` |
| `scope` | `all_agent_attempts_including_failures_and_repairs` |
| `replaces` | `upstream_agent_aggregate` |
| `amount_usd` | Finite nonnegative decimal amount |
| `event_id` | Unique billing-review event reference |
| `provider` | Provider reference |
| `source_sha256` | Hash of the reviewed billing evidence |
| `reviewer` | Review attribution |
| `allocations` | Optional complete task/trial allocation whose amounts sum exactly to the total |

Only one full-run agent reconciliation is accepted. Duplicate reconciliation,
incomplete allocations, wrong run binding, or ambiguous overlapping agent
charges are rejected. The displaced upstream aggregate remains visible. A
hash and reviewer reference are evidence of a recorded review assertion, not an
independent connection to a provider's billing system. Simulator, judge,
retrieval, infrastructure, and other declared components remain separate;
missing components continue to qualify the total and cost per success.
Explicitly attributed failed agent or retrieval requests enter agent operating
cost. A failed request with unknown attribution leaves that operating total
incomplete. A positive or unknown run-level charge without a task allocation
keeps per-task costs incomplete, even when other charges in that category have
been allocated.
If scheduled outcomes are incomplete, the observed-spend-per-observed-success
ratio has an incomplete denominator and is not labelled a lower bound on the
eventual experiment ratio. A lower-bound label applies only when the complete
outcome matrix fixes that denominator and cost components remain missing.

## Prepare a private custom-submission bundle

The command requires a complete final SAFE run: the canonical runtime, clean
recorded implementation revision, full domain, four or more trials, unchanged
source inventory, complete graded trajectories, saved judge outputs, and
matching saved-artifact hashes. Incomplete or infrastructure-failed attempts
remain in research reports; they cannot be silently removed to create a
submission. Finish or rerun the entire declared condition when appropriate,
retaining the earlier experiment in the audit record.

Create a private metadata JSON file with the actual model/developer/submitter
names, submission date, contact information, architecture explanation,
implementation URL, and development-exposure disclosure. The required keys are:

```json
{
  "model_name": "YOUR DISPLAY NAME",
  "model_organization": "ACTUAL MODEL DEVELOPER",
  "submitting_organization": "ACTUAL SUBMITTER",
  "submission_date": "2026-10-20",
  "contact_info": {"name": "ACTUAL CONTACT", "email": "REPLACE WITH ACTUAL EMAIL"},
  "architecture_notes": "Describe the frozen SAFE runtime, model route, admission and context changes.",
  "development_exposure_notes": "Disclose all task exposure, tuning, pilot selection, and freeze decisions.",
  "implementation_url": "https://github.com/AWDaley/SAFE-AGENT"
}
```

Replace every example value with the intended submission facts. No identity or
organization is inferred by the tool.

```bash
python -m safe_agent.benchmarks.tau3 prepare-submission \
  --run-dir artifacts/tau3/bank-final-safe \
  --metadata /absolute/private/submission-metadata.json \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output /absolute/private/new-submission-directory
```

The destination must not exist and must be outside the input run. The tool
checks the unchanged pinned public task definitions and trial counts, validates
the upstream submission schema, and regenerates official metrics from saved
rewards. It neither evaluates actions again nor reruns NL assertions. Duplicate
task/simulation IDs, changed reward bases, missing judge records, changed
metrics, and mismatched configuration are rejected.

The bundle contains `submission.json`, unchanged full `trajectories/` data,
`official_metrics.json`, configuration/development disclosures, and a final
`validation.json` with file hashes. It is marked `custom` with modified prompts.
Invoice cost is omitted from the submission score because this consistency
validator does not independently verify billing. The private runtime database,
provider keys, and host execution state are not copied.

The directory is owner-only; the completion record is created last. Public
validation establishes consistency with the pinned files and recorded rewards.
It does not authenticate a run independently, certify all semantic policies,
or imply leaderboard acceptance. Full trajectories intentionally contain the
benchmark conversation data. Review the complete bundle before any separate
publication decision. This command performs no upload, PR creation, message,
or leaderboard submission.
