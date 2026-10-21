# Pilot readiness and reservation calibration

`preflight` and `calibrate` make no model calls. The separate `run` command invokes
the configured agent, simulator, and unchanged official judge where required.
The implementation fixtures have not produced a model-backed pilot or score.

## Explicit matched development pilots

| Domain | SAFE configuration | Stock configuration | Task IDs |
| --- | --- | --- | --- |
| Retail | `configs/benchmarks/tau3-dev.toml` | `configs/benchmarks/tau3-dev-baseline.toml` | `0` through `4` |
| Banking | `configs/benchmarks/tau3-bank-dev-safe.toml` | `configs/benchmarks/tau3-bank-dev-baseline.toml` | `task_001` through `task_005` |

These are the first five tasks in each domain's pinned source order. Selection
uses IDs and source order, without choosing successful or easier outcomes.
Each pilot declares one trial and the same IDs in `development_task_ids`. Within
each pair, all configuration fields match except system, run ID, and output path.
Five tasks are a wiring and feasibility pilot, not an effectiveness estimate.

Template exposure is the intended selection. After execution, preserve every
attempted schedule and merge actual development exposure into later configurations.
Untouched templates are not completed experiments. No final configuration is
automatically edited and no illustrative budget allowance has been raised.

## 1. Check configuration without keys or a model service

Use the pinned environment from [TAU3_RUNTIME.md](TAU3_RUNTIME.md). From the SAFE
repository root, with `TAU3_SOURCE_ROOT` pointing at the verified checkout:

```bash
python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-bank-dev-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" --offline
python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-bank-dev-baseline.toml \
  --tau-root "$TAU3_SOURCE_ROOT" --offline
```

Offline preflight checks source and dependency provenance, full runtime-limit
schemas, supported model arguments, intended exposure, output-path availability,
and minimum reservation headroom. The minimum token hold includes the mandatory
request envelope; actual IDs and projected context require more. It reads pinned
judge/retry constants without importing the official harness or LiteLLM. It does
not read dispatch keys, contact sockets, create run directories, or reserve budget.

`status=configuration_validated` means the static checks pass; `ready_for_attempt`
remains false. Fixed issue codes explain a blocked check. Dirty development code
produces a warning; final runs require a clean recorded implementation revision.

## 2. Verify service configuration without inference

Configure the private model service and harness credentials using the runtime
runbook. Before importing the official harness, explicitly set:

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export LITELLM_MODE=PRODUCTION
```

The second setting disables LiteLLM's implicit `.env` loading. Endpoint overrides
are rejected by this initial hosted profile. Dispatch keys use the existing
owner-only file loader; key and credential values are never printed.

```bash
python -m safe_agent.benchmarks.tau3 preflight \
  --config configs/benchmarks/tau3-bank-dev-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT" \
  --output artifacts/tau3-readiness/banking-safe.json
```

The optional report path must be new and outside the intended run directory.
Reports have owner-only permissions and are never overwritten. Stock preflight
checks the harness without requiring SAFE's sidecar or applying SAFE budgets.

For SAFE, a fresh signed challenge goes to the private Unix socket. The signed
response binds the challenge, exact model, backend origin/settings, candidate and
instruction hashes, inference schemas, and protocol limits. Wrong keys, stale
responses, and incompatible descriptors are rejected. Readiness and inference
signatures have separate purposes. The exchange leaves dispatch fences and
inference budgets unchanged and never invokes the provider backend.

`status=ready_for_attempt` means the declared launch requirements pass. Harness
credentials are checked only for presence and the sidecar reports its configuration.
`provider_access_verified=false` remains explicit: key validity, account quota,
model access, response quality, and billing have not been tested. This is not
independent deployment or service attestation.

This first preflight profile covers explicit OpenAI model names. SAFE agent
arguments are `temperature` and `seed`; harness user/stock arguments additionally
support `num_retries` and `max_tokens`. Credentials in model arguments, endpoint
overrides, mock replies, and custom provider routes cannot pass this profile.
Local inference or a different judge needs a separately defined condition.

## 3. Execute the declared pilot

```bash
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-safe.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
python -m safe_agent.benchmarks.tau3 run \
  --config configs/benchmarks/tau3-bank-dev-baseline.toml \
  --tau-root "$TAU3_SOURCE_ROOT"
```

Canonical `run` repeats online preflight before creating its run directory or
constructing the official simulator. Rejection produces a structured report and
no run artifacts. A successful manifest records the current preflight and its
hash. A saved readiness report cannot replace the fresh check.

Injected/custom development runtimes retain their explicitly labelled fixture
path. Those records cannot support canonical calibration or submission.

### Retry and spending scope

| Component | Behavior in the checked-in condition |
| --- | --- |
| Official simulation wrapper | `max_retries=0` |
| SAFE provider backend | Automatic provider retries disabled |
| Stock agent and user simulator | Pinned upstream uses `num_retries=3` when absent or null |
| NL assertion judge | Pinned judge uses the same upstream provider-retry default |

Preflight lists these roles separately. Retry settings are not measured call
counts. SAFE reservations do not cap stock, simulator, judge, or infrastructure
invoices. The pinned retry logic is in
[the upstream generator](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/utils/llm_utils.py).

Offline inventory reports NL assertions on three selected retail tasks and none
of the selected banking tasks; the complete 97-task banking domain has one such
task. Selection is unchanged. The frozen NL judge remains
`gpt-4.1-2025-04-14`. Changing the agent alone does not make full evaluation free.

## 4. Calibrate from the actual saved SAFE pilot

```bash
python -m safe_agent.benchmarks.tau3 report \
  --run-dir artifacts/tau3/bank-dev-safe
python -m safe_agent.benchmarks.tau3 diagnostics \
  --run-dir artifacts/tau3/bank-dev-safe
python -m safe_agent.benchmarks.tau3 calibrate \
  --run-dir artifacts/tau3/bank-dev-safe \
  --target-config configs/benchmarks/tau3-final.toml \
  --headroom-factor 1.5 \
  --output artifacts/tau3-readiness/banking-calibration.json
```

The calibrator reads saved trajectories/hashes and a read-only snapshot of the
private ledger. It checks source, runtime, dependency and model conditions,
scheduled outcomes, provider usage, episode bindings, and durable evidence.
Terminal task failures remain included. Missing, scripted-only, ungraded,
interrupted, uncertain, or capped evidence yields `status=unavailable` and no
suggested patch. Missing pilot files never generate fixture-derived recommendations.

Eligible evidence uses the largest complete episode's held turns, model calls,
tool calls and token units, multiplied by the supplied factor and rounded upward.
Target run allowances scale by the target schedule. Cost allowances use the
caller's unchanged fixed per-attempt hold. Actual token counts cannot substitute
for the runtime's larger token reservations.

`status=review_required` includes derived requirements and a proposed patch with
only necessary cap increases. Existing caps are never lowered from a small pilot.
Deadlines, prompt/output limits, repairs, and the per-attempt hold are unchanged.
The command never edits configuration or authorizes spend. Its multiplier is a
planning assumption, not a confidence bound or completion guarantee. Reservation
amounts are not provider invoice estimates.

Review actual failures and settings, retain the exposure record, and freeze the
next condition. Coverage and the final four-trial comparison need separate real
runs. See [TAU3_REPORTS.md](TAU3_REPORTS.md) for comparison and submission commands.
