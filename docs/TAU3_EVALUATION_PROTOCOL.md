# Frozen τ³ text evaluation protocol

## Scope and status

The target is English text on τ³ v1.0.1, commit
`fc0055dc4e0a316c3f83133267fbd6faaa770992`. Retail is the integration milestone;
banking knowledge with BM25 is the primary comparison. τ-bench 2.0, voice,
AllTools, airline, and telecom are separate experiments.

An offline compatibility fixture demonstrates the integration contract. It is
not a model capability score. Completing all scheduled trajectories and reporting
their outcomes does not promise that every task succeeds.

## Reproduce the environment

Use Python 3.12 in the dedicated benchmark environment and install the frozen
benchmark dependency lock supplied with this change. The upstream requirement is
Python `>=3.12,<3.14`. Install the actual pinned τ source with its `knowledge`
extra, and keep the corresponding data directory with it. BM25 requires
`rank-bm25`; it does not require embeddings or the AllTools shell sandbox.

The benchmark environment and existing office runtime have separate dependency
constraints. In particular, the current upstream TAL dependency uses
`cryptography==45.0.7` while the office lock uses `46.0.4`. Do not silently replace
the office lock when preparing this environment.

The package includes a source manifest with Git blob hashes obtained from the
pinned upstream tree. The inspector authenticates all τ source files plus the
selected domain's text data. This works for a normal Git checkout or an exact
API-materialized checkout. A declared commit string alone is insufficient.
Missing/changed files, external symlinks, and additional banking task files fail
inspection. An installed package imported from another checkout is rejected.

From the SAFE repository root:

```sh
python -m safe_agent.benchmarks.tau3 inspect \
  --config configs/benchmarks/tau3-v1.0.1.toml \
  --tau-root ../tau2-bench-v1.0.1
```

Inspection performs no model calls. It reports observed task IDs, hashes, counts,
reward-basis counts, and public operation inventory. It does not emit task
instructions, reference answers, reference trajectories, or required documents.
The verified inventories are 114 retail tasks and 97 banking tasks; banking has
698 knowledge documents. Retail base contains 74 train and 40 test tasks. Banking
has no implemented held-out train/test split in this release.

## Runtime and authority boundary

The SAFE-owned CLI registers its agent factory before invoking the normal
`tau2.runner.run_domain(TextRunConfig(...))` in the same Python process.
Registration in a separate process is not sufficient. The factory discards the
upstream `Task`, including hidden scenario instructions, reference actions,
required-document lists, and evaluator metadata.

`protocol.py` defines JSON-only tools, messages, observations, runtime context,
and decisions. The root runtime receives tool schemas and declared effect
metadata, never official `Tool` callables or an environment/database object.
Its authorized decision becomes exactly one upstream `ToolCall`; only the
official orchestrator executes it. A matching result subsequently advances the
action journal. Duplicate call IDs, unmatched or repeated results, user-only
actions, and mixed text/tool outputs fail closed. The wrapper never executes a
tool and then returns the same call to τ.

Banking discovery remains a domain capability: retrieve documentation, unlock a
documented operation, validate its inner schema/effect, and emit
`call_discoverable_agent_tool` with an exact JSON argument string. Generic tools
such as unlock and user-tool grant can mutate state; `ToolType.WRITE` alone is
not a complete effect classification. Granting a user tool does not execute it
or establish success. The official user simulator owns user-only actions.

Retrieved documents and tool results cannot change SAFE authority. Customer
consent is bound to the applicable action and subject independently of operator
execution authority. Hidden task/evaluator files and result directories must
remain outside the planner's file access. Golden retrieval is not supported by
these configs.

## Development and final runs

Use authored offline fixtures for most work. Declare the IDs used for model
development and carry them into the final configuration's
`development_task_ids`. The inspector's selected IDs and each development run's
manifest are the exposure record. Do not call a full base run an untouched
holdout after tuning on its tasks.

The provided configurations are:

| Configuration | Purpose | Scheduled conversations |
| --- | --- | ---: |
| `tau3-dev.toml` | Five retail development tasks | 5 per system |
| `tau3-v1.0.1.toml` | Full banking coverage, one trial | 97 |
| `tau3-final.toml` | Full banking SAFE evaluation, four trials | 388 |
| `tau3-final-baseline.toml` | Matched stock LLMAgent evaluation | 388 |

For the retail coverage milestone, copy the coverage config, set
`domain="retail"`, and use a new run ID/output directory. It covers 114 tasks
once. The two final banking configurations schedule 776 conversations total.
These are conversation counts, not API request counts.

`run` invokes the selected model, official user simulator, and applicable
graders. Configure the supported SAFE inference route and providers first. No
model evaluation is implied by successful inspection or fixture tests.

```sh
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-dev.toml
```

Run the two final configurations after freezing code, model identifiers,
reasoning/temperature settings, prompts, policy/schema hashes, retrieval,
simulator, judge configuration, limits, and exposure record. Both systems must
use the same task set, trial schedule, permitted tools, retrieval profile,
simulator, and evaluator. Match seeds where supported; interactive trajectories
will diverge as assistant behavior changes.

The first wrapper is serial, sets no task filter, disables automatic review and
resume, uses zero retries, and enables communication-format validation for both
systems. A nonempty output directory is rejected rather than overwritten or
silently resumed. Interrupted trials stay visible; a deliberate new attempt
requires a new output directory and separate disclosure.

Coverage and final SAFE runs require the canonical `EpisodeRuntime` factory.
Injected or custom factories are allowed only in development and labelled in
the manifest as development implementations, so a scripted fixture cannot be
presented as a final SAFE comparison.

Official trajectories can contain native synthetic customer data. The wrapper
creates the run directory with owner-only permissions, applies a private file
creation mask during official execution, and restores the caller's mask even
on failure. It uses INFO logging with verbose transcript logging disabled.

## Evaluation and reproducibility

Keep upstream `EvaluationType.ALL` and each task's actual `reward_basis`.
Do not replace them with DB-only grading. At this pin, banking uses 87 DB tasks,
9 ACTION tasks, and 1 DB-plus-NL-assertion task. Some retail tasks also require
NL assertions. The pinned default NL judge is `gpt-4.1-2025-04-14` at temperature
zero. Generic evaluation documentation does not override the task files.

The wrapper writes `manifest.json` before execution, including the complete
task/trial matrix, pinned and working-source fingerprints, selected task/data
hashes, run configuration, budgets, exposure, and installed dependency versions.
The official runner writes its unchanged `results.json`; the wrapper stores
`official_metrics.json` from the upstream metric implementation. Both files may
contain evaluator-side information and must not be supplied to the planner.

Reporting reads recorded rewards and judge outputs. It never re-runs an LLM
judge. Fresh rejudging is a separate stochastic evaluation that can change the
score even with the same transcript.

```sh
python -m safe_agent.benchmarks.tau3 report \
  --run-dir artifacts/tau3/bank-final-safe
```

The output includes official metrics when available, a separately labelled
formula mirror, and conservative operational metrics. For a task with `n`
trials and `s` successful trials, the pinned estimator is
`C(s,k) / C(n,k)`. With four trials, pass⁴ counts tasks that succeed in all four.
It is not an at-least-one-success measure.

The official implementation excludes infrastructure errors and reduces the
available maximum k when remaining trials are uneven. Report scheduled,
attempted, terminal, graded, missing, duplicate, and infrastructure-failed
trials alongside that score. The conservative operational view keeps every
scheduled pair and counts missing, ambiguous, and infrastructure outcomes as
failures. Duplicate attempts invalidate the wrapper's score input instead of
selecting the best result. A grading failure is not silently converted into a
successful task.

## Cost accounting and limits

There are distinct agent, simulated-user, judge, and infrastructure costs. The
BM25 choice removes embedding charges, not agent/user/grader inference costs.
Offline fixtures use no billed model calls. Free/local model runs require a
compatible available backend and compute; they are separate model conditions
from paid published baselines.

The runtime's episode/run hard caps apply to SAFE's own inference and action
admission. They do not cap stock-baseline, user-simulator, or judge provider
billing. Those external calls have the official step/error bounds and observed
cost accounting. The example monetary limits are starting settings to review
after a pilot, not predicted run prices or an experiment-wide billing guarantee.

The example run allowances are deliberately small and are not sized to ensure
full coverage. `inspect` exposes `safe_reservation_headroom`: it divides the
configured allowance by the fixed per-attempt hold and applies the episode
ceiling across the scheduled matrix, assuming a fresh run with no prior usage.
Other token, call, time, and turn limits can reduce that ceiling further.

| SAFE configuration | Run allowance / per-attempt hold | Maximum attempts from these holds | Mean ceiling per scheduled conversation |
| --- | --- | ---: | ---: |
| Five-task retail development | $10 / $0.10 | 100 | 20 |
| 97-task banking coverage | $10 / $0.10 | 100 | About 1.03 |
| 388-conversation banking final | $100 / $0.10 | 1,000 | About 2.58 |

Each example also limits one episode to 100 attempts through its $10 allowance
and $0.10 hold. Multi-step conversations can exhaust these settings before the
planned matrix completes. Calibrate limits from a small pilot's actual attempt
and reservation usage before freezing coverage/final configs; changing a cap
changes the evaluation condition and belongs in the manifest. Malformed known
replies and uncertain attempts retain their holds. These figures are allowance
arithmetic, not expected API usage or estimated invoices. The stock baseline
ignores SAFE's budget sections, which do not constrain its provider charges.

`results.json` supplies recorded agent/user aggregates. Missing costs remain
unknown rather than zero. For charges not included there, add an
`additional_costs.jsonl` in the run directory with one record per distinct cost:

```json
{"category":"judge","amount_usd":"0.0123","source":"provider usage export"}
{"category":"failed_request","amount_usd":null,"source":"billing outcome unresolved"}
```

Supported additional categories are judge, review, retrieval, infrastructure,
agent_extra, and failed_request. Do not add agent/user aggregates again. Known
totals and missing/unreported components are separate; cost per successful
attempt includes failed attempts' recorded spend and is undefined at zero
successes. An incomplete cost ledger is labelled as a lower bound.

SAFE emits token/attempt provenance and reserved cost holds separately. It does
not have independently attested provider-dollar charges in this integration.
The upstream aggregate can turn absent message costs into zero; the wrapper
therefore explicitly flags `agent_provider_billing` as unreported for SAFE.
Reserved microdollar allowances, including holds in scripted tests, are not
actual spend and are never added to the known billed total.

## Separate runtime diagnostics

The diagnostics command reads the local durable episode database and exports
aggregate evidence separately from official task rewards:

```sh
python -m safe_agent.benchmarks.tau3 diagnostics \
  --run-dir artifacts/tau3/bank-final-safe
```

This reads `private/episodes.sqlite` and writes `diagnostics.json` with owner-only
file permissions. Alternatively, use `--database PATH` to print aggregates
without writing a file, optionally adding `--output PATH` and `--run-id ID`.
It never invokes a model, user simulator, tool, or judge. SQLite is opened in
read-only mode, with a query-only transaction for one consistent snapshot.
The journal is replayed using the pure reducer, without the store's writer-lock
replay method. The command does not recover, retry, or change episode state.
Exit status zero means the reported integrity checks passed; it does not mean
the episodes succeeded. Unavailable evidence or integrity failures return two.

The output contains only counts, fixed reason/state labels, rates, and latency
summaries. It excludes native arguments, customer text, tool/model output,
episode/call/request identifiers, hashes, reviewer identities, and raw exception
messages. Unknown stored reason strings are grouped as `OTHER`, because even
an uppercase reason string could contain private data.

Reported measures include:

- Episodes by replay-verified state, missing private contexts, and active
  processing claims.
- Prepared tool calls, known success/failure results, outstanding results,
  uncertainty ever recorded, unresolved uncertainty, and later reconciliation.
- Exact persisted authorization bindings matched to preparation-event hashes;
  presence or absence of domain-admission and customer-consent receipts.
- Original domain-proposal denials by reason, separately from repeated repair
  feedback. A customer-consent request is not automatically a false refusal.
- Reserved model attempts, durable dispatch states, known replies, schema
  acceptance/rejection, usage provenance, lifetime held tokens and cost, and
  gaps between separate persistence steps. A saved reply without a journal
  finish remains distinguishable from an unknown reply.
- Host-observed inference reservation-to-finish and tool prepare-to-result
  durations, with sample counts, minimum, mean, nearest-rank p50/p95/p99, and
  maximum. Unfinished pairs remain counted and are excluded from latency
  samples; they are not zero-duration completions. These intervals include
  orchestration and persistence and are not isolated provider timings.
- Replay, snapshot, result/receipt binding, private-context, model-attempt,
  run-budget, and orphan-record integrity failures. Invalid episode journals
  remain counted as failures and are excluded from verified episode metrics.

Receipt presence does not establish semantic correctness of authorization or
prove that consent was required for a particular operation. Prepared calls do
not by themselves prove external execution. The report leaves actual external
dispatch counts and actual billed model dollars unknown. Private-context denial
counts cover persisted diagnostics; a crash before saving that context may omit
diagnostics from the interrupted step. Integrity checks detect inconsistent
evidence, not a privileged attacker who can coherently rewrite the whole store.

False-refusal rates remain unknown without explicit reviewed annotations. A
reviewer can supply a private JSONL file using `--annotations PATH`. Each line
must contain `schema_version="safe_agent.denial_review.v1"`, `episode_id`,
`diagnostic_index`, `proposal_hash`, `reviewed=true`, `reviewer_id`, an aware
`reviewed_at` timestamp, and a `label` of `false_refusal`, `correct_denial`, or
`not_assessable`. The identifier/index/hash must match the original persisted
proposal-denial entry; repair feedback, duplicates, stale bindings, and
unreviewed entries are rejected. Reviewer identifiers and source references
remain private.

The reported reviewed-sample false-refusal rate is false refusals divided by
false refusals plus correct denials. Its unit is a denied domain proposal, not
a user request or benchmark task. An overall recorded-denial rate is available
only when every recorded denial has an assessable review. Partial reviews are
labelled as a subset and must not be presented as the full population's rate.

## Interpretation and next comparison

Compare SAFE with the actual stock LLMAgent using the same underlying model.
That isolates scaffold and policy effects more cleanly than comparing unrelated
model/retrieval conditions. Claims about a named competitor product require its
own adapter and matched run. Report success, cost, latency, unauthorized-action
attempts and dispatches, consent failures, duplicate effects, uncertainty, and
false refusals separately from official reward.

A full-domain run with honest failures can complete the experiment. Model
success, safety performance, and superiority to a competitor remain measured
outcomes. Prepare a custom submission package for the changed scaffold; public
submission is a separate publication action.

## Primary pinned references

- [Agent interface](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/agent/base_agent.py)
- [Runner construction and privileged task boundary](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/runner/build.py)
- [Official runner and evaluation default](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/runner/batch.py)
- [Metric implementation](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/metrics/agent_metrics.py)
- [Banking tools and discovery](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/domains/banking_knowledge/tools.py)
