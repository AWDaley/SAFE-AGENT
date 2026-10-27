# Running SAFE-AGENT on the frozen τ³ text benchmark

## What this implementation delivers

SAFE now has a separate, durable customer-service runtime for the English-text
τ³ v1.0.1 benchmark. The official τ orchestrator remains the sole business-tool
executor. SAFE receives public policy, tool schemas, and conversation observations;
it journals, checks, and emits one proposed action at a time. The retail and
banking integrations are exercised with authored, offline conversations against
the real pinned harness.

This is implementation and integration evidence. It is not a completed
97-task model evaluation, a competitive score, or a claim of complete natural-
language policy enforcement. Model routes include fixed-origin hosted OpenAI and
an explicit loopback OpenAI-compatible service for local development pilots.
Neither route has produced a model-backed benchmark score in this implementation.
All fixture tests, source inspection, and diagnostics can run without a model key
or paid API call.

The target and dependency revisions are deliberately separate:

| Component | Revision or profile |
| --- | --- |
| Official τ³ text source/data | `fc0055dc4e0a316c3f83133267fbd6faaa770992` (v1.0.1) |
| Benchmark TAL admission | `ci/tal-benchmark-commit.sha` |
| Existing office TAL runtime | Unchanged `ci/tal-office-commit.sha` |
| Benchmark dependencies | `requirements-tau3.lock`, Python 3.12 |
| Existing coordinated office dependencies | Unchanged `src/safe_agent/runtime/coordinated-linux-py312.lock` |

Do not install the two TAL revisions into one environment. The benchmark lock
pins the resolved application dependencies and the two VCS commits. It does not
hash every downloaded wheel or freeze all build-system tooling.

## Free setup and verification

Run these commands from the SAFE repository root in a credential-free shell.
The environment checker intentionally rejects provider credential environment
variables. Installation downloads packages and public source; no model inference
is involved.

```bash
python3.12 -m venv .venv-tau3
. .venv-tau3/bin/activate
python -m pip install -r requirements-tau3.lock
python -m pip check

git clone --no-checkout https://github.com/sierra-research/tau2-bench.git \
  ../tau2-bench-v1.0.1
git -C ../tau2-bench-v1.0.1 checkout --detach \
  fc0055dc4e0a316c3f83133267fbd6faaa770992

export TAU3_SOURCE_ROOT="$(cd ../tau2-bench-v1.0.1 && pwd)"
export TAU2_DATA_DIR="$TAU3_SOURCE_ROOT/data"
export PYTHONPATH="$TAU3_SOURCE_ROOT/src"
export LITELLM_LOCAL_MODEL_COST_MAP=True
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

python scripts/check_tau3_environment.py --tau-root "$TAU3_SOURCE_ROOT"
python scripts/generate_tau_domain_catalog.py "$TAU3_SOURCE_ROOT" --check
python -m pytest tests/episodes tests/domains tests/benchmarks/tau3 \
  --ignore=tests/episodes/test_episode_postgres.py
python -m ruff check src tests scripts \
  docker/episode_service_probe.py \
  migrations/versions/0015_benchmark_episodes.py \
  migrations/versions/0016_episode_inference.py
python -m mypy src/safe_agent
```

The source checker requires the actual noneditable, commit-pinned TAL package.
It also verifies installed τ metadata, the imported checkout, its source/data
hashes, the generated public-operation catalog, and the counts for both domains.
It does not feed task answers or evaluator records to SAFE.

Inspect a complete run schedule without starting inference:

```bash
python -m safe_agent.benchmarks.tau3 inspect \
  --config configs/benchmarks/tau3-v1.0.1.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
```

Expected inventory: 114 retail tasks; 97 banking tasks and 698 banking knowledge
documents. The banking coverage configuration schedules 97 conversations. The
two final banking configurations schedule 388 each, 776 combined. Inspecting a
schedule does not execute those conversations.

For a five-task development selection, use the paired templates and offline
`preflight` commands in [the pilot workflow](TAU3_PILOTS.md). Offline preflight
checks launch configuration without reading keys, contacting a service, or
creating run artifacts. It does not establish provider access.

### What the offline tests establish

The tests cover the actual SAFE runtime and official retail/banking tool
interfaces, not just a stub adapter. Authored examples include consent-bound
retail cancellation, and banking BM25 retrieval followed by documented tool
discovery/unlock. Other checks exercise model-output rejection, scoped references,
result correlation, admission denial, stale/concurrent state, finite budgets,
crash recovery, immutable evidence, and the independent sidecar's dispatch fence.

The scripts use synthetic records and scripted model replies. A fixture's reward
does not belong in a model leaderboard or in the official full-domain score.

### PostgreSQL acceptance

The canonical benchmark runner currently uses an isolated SQLite database per
run. PostgreSQL support in the durable store has a separate mandatory CI job in
`.github/workflows/tau3.yml`; it exercises real PostgreSQL triggers, concurrent
budget admission, and recovery. SQLite is not a substitute for that acceptance.

For a disposable local PostgreSQL database, set `SAFE_AGENT_DATABASE_URL` to that
database, install this same lock, and run:

```bash
python -m alembic upgrade head
SAFE_AGENT_TEST_POSTGRES_EPISODES=1 \
  python -m pytest tests/episodes/test_episode_postgres.py
```

The opt-in suite deliberately mutates its test database. Use a disposable test
instance. The CI gate rejects skipped tests in this suite.

### Container attempted-read acceptance

The separate Linux Docker profile supplies the implementation plan's free
process-isolation probe for one explicit image and launcher. From a nonroot
Linux account with access to the local Docker Engine, run:

```bash
SAFE_EPISODE_BUILD_DIR="$(mktemp -d)"
python scripts/episode_service_container.py prepare-context --output "$SAFE_EPISODE_BUILD_DIR"
docker build --pull --tag safe-episode-model:review "$SAFE_EPISODE_BUILD_DIR"
SAFE_EPISODE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' safe-episode-model:review)"
python scripts/episode_service_container.py probe \
  --image "$SAFE_EPISODE_IMAGE_ID" --output episode-container-probe.json
```

This boots the real sidecar with synthetic keys and network disabled. It tests
service health and unauthenticated rejection, then attempts six reads of
unmounted host evaluator and harness-secret canaries from inside the container.
It verifies the intended key reads, three exact mounts, environment separation,
nonroot identity, dropped capabilities, no-new-privileges, and read-only root.
The image uses a positive source allowlist and contains no benchmark package,
data, evaluator, or run artifacts. No successful inference or model call occurs.

The `container-isolation-probe` CI job is mandatory. Its result applies to this
profile, not the bare Python launch below. See
[TAU3_CONTAINER_PROFILE.md](TAU3_CONTAINER_PROFILE.md) for the exact deployment
command, dedicated state-directory checks, supported host setup, and limits.
Enabling bridge networking for a hosted pilot is a separate choice; the offline
probe does not establish live egress confinement or generic hostile-code
containment.

## Runtime boundaries

| Boundary | Implemented behavior |
| --- | --- |
| Official adapter | Discards privileged `Task` data; accepts JSON-only observations; emits one call; leaves execution to τ. |
| Durable episode store | Run and episode budgets, version checks, hash-linked events, one pending action, result correlation, immutable call/authorization binding. |
| Private conversation store | Owner-only run files, scoped references, source-linked private values, consent proposals, and recovery state. |
| Domain admission | Reviewed TAL decision plus encoded retail/banking rules for identity, ownership, operation/schema, discovery, and applicable consent. |
| Model sidecar | Signed finite requests over a Unix socket; declared hosted origin or validated loopback endpoint and model; no automatic provider retry; durable request fence. |
| Completion renderer | Mutation success requires a matching successful tool receipt; recipient-scoped private interpolation. |
| Reporting | Official recorded rewards and metrics, complete schedule accounting, separate runtime diagnostics and cost uncertainty. |

An admitted call is written with its exact native arguments, effect hash, current
observation/policy/version binding, and TAL admission hash before the adapter can
return it. A duplicate or recovered prepared call is not emitted a second time.
A crash after preparation can leave an action uncertain, including when no tool
effect actually happened. This conservative outcome is visible in diagnostics;
it is not silently retried or converted into success.

Model attempts similarly reserve budget and persist their request before
dispatch. A transport timeout or interrupted claim holds the reservation and
ends in uncertainty. A known malformed response can take a separately budgeted,
bounded repair attempt. The host never refunds an unknown bill automatically.

The `tau3-observed-evidence-v1` context profile keeps correlated BM25 document
IDs, content hashes, literal source spans, retrieval-call provenance, and complete
observed unlock definitions. It retains the current subject, exact pending
action/confirmation bindings, applicable consent, and first/latest customer
turns before adding optional recent evidence and history. It never imports an
unseen discoverable schema or summarizes facts with another model. Span offsets
and hashes refer to the original observation; text is privately projected before
model delivery. Omission counts and hashes make compaction visible.

Literal excerpts use 768-character windows around a document's prefix, active
operation names, and a deterministic query-term window, with at most eight
optional recent document versions. This is a bounded retention rule, not a
guarantee of semantic retrieval relevance. Required evidence that cannot fit
within `max_prompt_bytes` stops before provider admission. The profile is bound
into run and policy hashes so an old run cannot silently resume under new
retention rules.

Consent is customer dialogue state bound to the exact pending action. It does
not grant operator or execution authority. Banking's user-only tools remain with
the official user simulator. Retrieved knowledge and model text cannot create
tools or authorization.

The domain packs enforce the coded structural and business constraints. They do
not formally verify every rule expressed in the banking knowledge base. Private
projection covers known pinned field formats and source-linked values; it is not
a general-purpose PII classifier. The sidecar process and opaque references do
not by themselves establish OS sandboxing, encrypted-at-rest storage, network
egress controls, or production deployment approval.

## Preparing a hosted pilot

Complete the free checks first. The `run` command can incur agent, simulated-user,
and judge charges. The provided dollar settings are reservation allowances, not
price estimates or verified provider billing caps. No current prices are assumed.

Before a model-backed run, set the exact model IDs and arguments in a copied
configuration, choose a new `run_id` and empty `output_dir`, and set your intended
limits. The SAFE backends accept `temperature` and optional `seed`. To declare
matched agent transport controls, SAFE also accepts `num_retries=0` and
`max_tokens` equal to `episode_budget.max_output_tokens`; neither setting can
change SAFE's retry policy or raise its output cap. Nonzero/null/boolean retries
and conflicting output caps fail before dispatch. Other model-specific settings
require an explicit backend implementation and a new frozen comparison condition.

The [pilot workflow](TAU3_PILOTS.md) lists matching SAFE and stock templates for
retail IDs `0`–`4` and banking IDs `task_001`–`task_005`. These fixed source-order
selections are declared in both `task_ids` and `development_task_ids`; loading a
template does not mean its pilot has been executed.

### 1. Configure the model-only sidecar

Choose the constrained container launch documented in
[TAU3_CONTAINER_PROFILE.md](TAU3_CONTAINER_PROFILE.md), or the trusted-host Python
launch below. Only the container launch applies the tested mount and process
restrictions.

Provision three owner-controlled paths outside the repository and output
directories:

- An absolute service directory owned by the current user with mode `0700`.
- A dispatch-key file, mode `0600`, containing a randomly generated ASCII secret
  of 32–512 characters. The runtime and sidecar use the same file.
- A separate agent-provider-key file, mode `0600`, containing the OpenAI API key.
  The sidecar reads it; never put its content in configuration files or prompts.

Keep the key files in an owner-controlled directory with mode `0700` as well.

Set paths, not key values, and start the service in its own terminal:

```bash
export SAFE_EPISODE_BACKEND=hosted_openai
unset SAFE_EPISODE_LOCAL_BASE_URL
export SAFE_EPISODE_SERVICE_DIR=/absolute/private/service-directory
export SAFE_EPISODE_DISPATCH_KEY_FILE=/absolute/private/dispatch-key
export SAFE_EPISODE_OPENAI_KEY_FILE=/absolute/private/agent-provider-key
export SAFE_EPISODE_MODEL=gpt-4.1-2025-04-14
python -m safe_agent.inference_service.episode_service
```

The socket is created at `$SAFE_EPISODE_SERVICE_DIR/episode.sock`. The service
stores a durable provider-request fence in the same directory. Keep that state
through restarts. Deleting it discards the service's duplicate-request history.

The hosted backend fixes the origin to `https://api.openai.com/v1`, disables
automatic retries, redirects, and environment proxy inheritance, and validates
the model against the configured route. The provider key is absent from the
projected candidate request. This is a trusted-host deployment profile, not a
general untrusted-code sandbox.

### 2. Configure the benchmark host

In the benchmark terminal, activate `.venv-tau3`, retain the verified τ paths,
and set:

```bash
export SAFE_EPISODE_SOCKET=/absolute/private/service-directory/episode.sock
export SAFE_EPISODE_DISPATCH_KEY_FILE=/absolute/private/dispatch-key
export LITELLM_LOCAL_MODEL_COST_MAP=True
export LITELLM_MODE=PRODUCTION
```

`LITELLM_MODE=PRODUCTION` disables implicit `.env` loading by the frozen harness's
LiteLLM dependency. Hosted preflight rejects endpoint overrides and
credentials supplied inside model arguments.

The official user simulator and any NL judge also need provider credentials in
their trusted harness process, typically `OPENAI_API_KEY` for the example model.
Provision those through your normal secret mechanism. They are not given to
SAFE's projected model context. The offline checker is intentionally run before
adding these credentials, because its credential-free contract is stricter than
the live harness's requirements.

### 3. Check readiness, then run a small declared pilot

This example uses the five-task retail SAFE template. Its matched stock template
is `configs/benchmarks/tau3-dev-baseline.toml`; use the same preflight/run sequence
for that configuration. The paired banking commands are in
[TAU3_PILOTS.md](TAU3_PILOTS.md).

```bash
python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-dev.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-dev.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 report \
  --run-dir artifacts/tau3/retail-development
python -m safe_agent.benchmarks.tau3 diagnostics \
  --run-dir artifacts/tau3/retail-development
```

Online preflight authenticates the sidecar's declared model/protocol without
inference and checks harness credential presence. `provider_access_verified=false`
remains explicit: provider key validity, quota, model access, and billing have
not been tested. Canonical `run` repeats this check before creating the output
directory or constructing the official simulator. A saved report cannot bypass
the fresh gate; a blocked check creates no run artifacts.

Pilot exposure must be recorded. Use the selected IDs in `manifest.json` to
populate `development_task_ids` before freezing final configs. Diagnose actual
failures before widening the run. If a known benchmark policy requirement is
unsupported, improve the corresponding general rule and rerun authored fixtures;
do not insert expected task actions or answers into the runtime.

A nonempty output directory is rejected. The simulation wrapper uses
`max_retries=0`, and SAFE's provider backend disables automatic retries. The
unchanged upstream stock agent, user simulator, and NL judge use provider
`num_retries=3` when absent or null. These are separate retry scopes, not measured
call counts. Automatic review and resume are disabled. Use a new run ID and
directory for an explicit new experiment; retain interrupted schedules and outcomes.

## Preparing a local development pilot

The first local attempt and the new normalized reliability condition are
documented in [TAU3_LOCAL_RELIABILITY.md](TAU3_LOCAL_RELIABILITY.md). Follow that
runbook for explicit coordinated timing, bounded live qualification, semantic
review, and stopping after the first infrastructure failure. The original
templates and legacy timing below remain available for reproducibility.

The local option sends the SAFE agent and official user simulator to a local
OpenAI-compatible service; its paired stock run uses the same service and model.
The supplied banking pair selects `task_001`–`task_005`, exactly as the hosted
pilot does. These five pinned tasks have no NL assertions. Local preflight rejects
any selection requiring the unchanged hosted judge, and local runs are limited to
`stage="development"`. Full coverage, final runs, and submission preparation
retain the hosted route; no judge is substituted or assertion removed.

With locally installed weights and cloud features disabled, this path needs no
hosted model account or API payment. Hardware, electricity, storage, download
time, and inference time remain real costs. A loopback URL establishes the client
destination; it does not attest where a server executes the model or constrain
that server's own network access. No weights have been downloaded or actual
Ollama model quality measured by the implementation fixtures.

### 1. Prepare a local model server

Install [Ollama](https://docs.ollama.com/quickstart) separately on a host with
enough RAM or VRAM for your model and context. The templates use
[`qwen2.5:7b`](https://ollama.com/library/qwen2.5), a tool-capable example, not a
validated recommendation for task success. SAFE needs JSON output; the stock
agent and user simulator also need native tool calls. Ollama documents
[OpenAI-compatible chat, tools, and JSON fields](https://docs.ollama.com/api/openai-compatibility)
and [structured output support](https://docs.ollama.com/capabilities/structured-outputs).

For a standalone server, use a dedicated terminal:

```bash
OLLAMA_HOST=127.0.0.1:11434 \
OLLAMA_NO_CLOUD=1 \
OLLAMA_CONTEXT_LENGTH=32768 \
OLLAMA_NUM_PARALLEL=1 \
ollama serve
```

If Ollama already runs as an application or service, configure that process and
restart it instead of starting a second server. The official
[server configuration and cloud-disable guidance](https://docs.ollama.com/faq)
describes each platform. Keep the server bound to loopback and disable its cloud
features. The 32,768-token context is an explicit example, not a measured capacity
requirement or a completion guarantee. Larger contexts consume more memory; see
[Ollama's context guidance](https://docs.ollama.com/context-length). SAFE's prompt
limit is in bytes and does not establish that a model's token context is sufficient.

In another terminal, download the example only if it is not already installed,
then record the model and server metadata:

```bash
ollama pull qwen2.5:7b
ollama --version
ollama list
ollama show qwen2.5:7b
```

The pull downloads model files; it is separate from inference. Preserve the
resolved model digest, quantization, server version, context setting, and host
hardware with the experiment notes. A model tag alone can change. Preflight does
not verify the installed weights, their digest, available memory, tool behavior,
or server liveness by sending inference.

### 2. Configure the local SAFE sidecar

Provision the owner-only service directory and dispatch-key file described in
the hosted setup above. Local mode needs no agent-provider-key file. Start its
trusted-host service in the same network namespace as Ollama:

```bash
export SAFE_EPISODE_BACKEND=local_openai
export SAFE_EPISODE_LOCAL_BASE_URL=http://127.0.0.1:11434/v1
export SAFE_EPISODE_MODEL=qwen2.5:7b
export SAFE_EPISODE_SERVICE_DIR=/absolute/private/local-service-directory
export SAFE_EPISODE_DISPATCH_KEY_FILE=/absolute/private/local-dispatch-key
python -m safe_agent.inference_service.episode_service
```

This backend reads no OpenAI key file and sends no Authorization header. It uses
the declared loopback endpoint with redirects, environment proxies, and automatic
retries disabled. The signed capability exchange binds the declared endpoint and
raw model alias. Keep its durable fence through restarts, as for hosted mode.

The offline no-network container probe remains a separate acceptance profile.
A container's `127.0.0.1` is not the host's Ollama service. This local walkthrough
does not claim the container profile's mount or process isolation.

### 3. Check and run the matched local pair

Activate the pinned benchmark environment and retain its verified source/data
paths. Use a credential-free benchmark shell and set:

```bash
export SAFE_EPISODE_SOCKET=/absolute/private/local-service-directory/episode.sock
export SAFE_EPISODE_DISPATCH_KEY_FILE=/absolute/private/local-dispatch-key
export LITELLM_LOCAL_MODEL_COST_MAP=True
export LITELLM_MODE=PRODUCTION

python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-bank-dev-local-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-bank-dev-local-baseline.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-local-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-local-baseline.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
```

Add `--offline` to preflight for static checks before installing or starting a
model server. Online SAFE preflight authenticates the sidecar; stock preflight
does not require it. Online local preflight checks the server's bounded model
list for both configured aliases, without loading weights or generating text.
`provider_access_verified=false` remains explicit: alias presence is not a test
of inference, JSON/tool behavior, or locally executed weights. The first `run`
is the actual model attempt.

Both templates explicitly set `model_route="local"`,
`local_api_base="http://127.0.0.1:11434/v1"`, and the same raw agent/user model
alias. Use aliases without `/`; do not put endpoints or credentials in
`llm_args_agent` or `llm_args_user`. The trusted harness adapter adds its provider
prefix, local endpoint, nonsecret compatibility key, and zero provider retries.
That placeholder is not an account credential. Change the model in both copied
configs and the sidecar together, with new run IDs/output paths, for a new pair.

The existing SAFE reservation units remain finite and unchanged: the supplied
run hold permits at most 100 SAFE model attempts across five conversations. These
units are not local API charges or hardware-cost estimates. Record all failed,
uncertain, capped, and incomplete attempts. Local server-reported tokens do not
establish zero total experiment cost. Compare the local pair with each other;
different local and hosted model conditions cannot isolate SAFE's runtime effect.

## Reading the evidence

Each run writes a manifest before invoking the official runner. It contains the
full scheduled task/trial matrix, source/config fingerprints, model settings,
retrieval condition, limits, and the fresh preflight report/hash. Official outputs
remain evaluator-side.

`report` reads recorded rewards without rerunning a judge. It distinguishes
missing, duplicate, infrastructure-failed, ungraded, and scored conversations.
The conservative operational view retains every scheduled pair. The pinned
pass^k estimator is `C(successes, k) / C(trials, k)` for each task: with four
trials, pass^4 measures success on all four, not success at least once.

`diagnostics` reads a consistent SQLite snapshot without advancing or repairing
the runtime. It emits aggregate states, evidence-integrity results, tool/model
attempt states, admission-related counts, latencies, and reservation/usage
provenance. It excludes native customer text and tool arguments. An attached
review annotation can support a false-refusal count; an admission denial alone
cannot establish that the refusal was wrong. See the diagnostics section of
[the evaluation protocol](TAU3_EVALUATION_PROTOCOL.md) for its annotation schema.

Source-bound authorizations and hash consistency are mechanical evidence. They
do not prove all actions were semantically correct. Compare those diagnostics
with official task outcomes and a reviewed sample of failures.

The benchmark harness can aggregate an absent message cost into zero. SAFE's
report explicitly retains the missing agent-provider billing component. Reserved
microdollar allowances and provider-reported tokens are separate quantities;
they are not independently reconciled invoice charges. Record user, judge,
failed-request, and infrastructure charges separately when available. Full costs
and cost per success remain qualified while components are unknown.

## Moving from the pilot to the comparison

Run `calibrate` on the actual saved SAFE pilot before choosing coverage/final
allowances; see [TAU3_PILOTS.md](TAU3_PILOTS.md) and
[the saved-result guide](TAU3_REPORTS.md). It retains complete natural task failures
and withholds suggestions for incomplete, uncertain, capped, or scripted evidence.
Eligible evidence produces only proposed cap increases; deadlines, prompt/output
limits, repair limits, and the per-attempt hold remain unchanged. It never edits
configs, estimates invoices, or authorizes the next run.

After the pilot, freeze code, dependencies, prompt/schema/policy hashes, exact
agent/user/judge model settings, exposure record, BM25 settings, and limits.
Use the 97-task one-trial banking configuration to check complete runner coverage.
Then use `tau3-final.toml` and `tau3-final-baseline.toml` for the matched four-trial
comparison. Preserve official `EvaluationType.ALL`, including applicable NL
assertions, for both systems.

Compare the saved runs without making new model or judge calls:

```bash
python -m safe_agent.benchmarks.tau3 compare \
  --safe-run-dir artifacts/tau3/bank-final-safe \
  --baseline-run-dir artifacts/tau3/bank-final-baseline \
  --output artifacts/tau3/bank-comparison.json
```

The [saved-result analysis guide](TAU3_REPORTS.md) documents paired uncertainty
intervals, reviewed failure categories, agent-billing reconciliation, and the
`prepare-submission` command. A submission package requires real complete final
results and explicit submitter metadata; packaging does not publish it.

The stock baseline uses the same underlying model under matched benchmark
conditions. The comparison measures differences between complete systems,
including prompts, planning, tool projection, repair behavior and SAFE's limits.
It does not isolate the causal effect of enforcement. A claim about a different
named competitor requires that competitor's supported adapter, the same
task/evaluator condition, and its own recorded run.

The first local pilot attempted five tasks but graded none; its evidence and
follow-up are in [the reliability runbook](TAU3_LOCAL_RELIABILITY.md). Remaining
empirical work includes a successfully qualified and graded local pilot, hosted
pilots, complete retail/banking
coverage runs, calibration of limits from observed usage, four-trial matched
banking evaluation, reviewed false-refusal/policy outcomes, and provider-billing
reconciliation. No leaderboard submission or production rollout is part of the
offline implementation.

## Code map

- [Original implementation plan](TAU3_IMPLEMENTATION_PLAN.md)
- [Evaluation protocol](TAU3_EVALUATION_PROTOCOL.md)
- [Pilot readiness and reservation calibration](TAU3_PILOTS.md)
- [Saved-result analysis and private submission guide](TAU3_REPORTS.md)
- [Domain policy coverage and limits](benchmark-domains.md)
- [Adapter and runner](../src/safe_agent/benchmarks/tau3/)
- [Runtime](../src/safe_agent/orchestration/episode_runtime.py)
- [Episode storage](../src/safe_agent/storage/episodes.py)
- [Inference gateway](../src/safe_agent/inference/episode_gateway.py)
- [Model-only sidecar](../src/safe_agent/inference_service/episode_service.py)
- [Benchmark CI](../.github/workflows/tau3.yml)
