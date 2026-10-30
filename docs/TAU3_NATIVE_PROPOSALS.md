# Local native proposal interface

`native_tools_v1` is an **opt-in agent presentation condition**. It addresses a
specific observed failure: the local agent returned ordinary messages promising
retrieval even though a suitable read tool and its arguments were available.
It does not establish that a model can choose appropriate tools, follow policy,
maintain the customer role, or qualify for an official benchmark run.

The existing `candidate_json_v1` route remains the default. Hosted inference,
the fixed τ³ v1.0.1 customer simulator, and the evaluator are unchanged.

## Proposal boundary

The local sidecar presents the already projected agent tool catalogue as native
Chat Completions functions. Function names, descriptions and parameter schemas
are preserved. The provider's user message retains every other projected context
field; a separate `native_tool_metadata` field retains tool effects and other
catalogue metadata. No historical customer or tool text is promoted into the
system instruction. Discovered or user-only tool implementations are not added
to the function catalogue.

One extra function, `safe_agent_message`, represents the existing message
candidate. Its arguments retain `purpose`, `text`, `evidence_call_ids` and `stop`.
`tool_choice=required` therefore still permits a reply, question or completion
proposal. It does not require a domain read or mutation. Parallel calls are
disabled.

The adapter accepts exactly one listed function call with `finish_reason` equal
to `tool_calls`. It translates the result into the unchanged SAFE candidate
union. An ordinary reply outside a function, mixed reply and call, multiple
calls, unknown function, refusal, duplicate JSON key, nonfinite value, oversized
argument object or invalid message candidate is rejected. Provider call IDs are
discarded; they cannot create host receipts or authorization.

The sidecar never executes a provider function. The existing trusted runtime
still validates domain arguments, resolves opaque references, checks TAL and
exact-action consent, dispatches an admitted operation, and correlates its real
result. The existing success-message receipt guard remains in force.

The v1 native catalogue supports at most 127 supplied functions plus the message
function. Names must match `[A-Za-z][A-Za-z0-9_]{0,63}` and have object parameter
schemas. Unsupported names, duplicate names, the reserved message-function name,
or an existing `native_tool_metadata` context field fail before a provider
request. The adapter does not silently rename tools or fall back to JSON mode.

## Condition identity and failure behavior

The selected format is bound into benchmark configuration, qualification
conditions, runtime configuration hashes, and saved route provenance. A run
cannot resume under the other format. The authenticated sidecar capability check
expects `local_openai_tools_v1`, `response_format=native_tools`, and a distinct
system-instruction hash binding the native protocol and message-function schema.
The candidate schema hash stays the same.

The native condition is limited to the canonical local SAFE development route.
An attempt to select it for hosted inference, a stock baseline, or a custom
runtime is rejected. The default format is omitted from legacy serialization so
existing default configurations retain their representation.

Provider deadlines, response-size limits, output caps, zero retries, transport
cancellation and durable uncertainty handling are shared with the existing
local backend. A completed but malformed native response is treated as provider
uncertainty: it does not use the legacy candidate-JSON schema-feedback repair
path. No automatic retry, alternative model, synthetic success, reservation
release or replay is introduced.

The interface changes the function presentation, instruction and response
translation together. A live comparison is a comparison of those complete
interface conditions, not evidence that any one wording change caused an effect.

## Selecting the condition

Start from `configs/benchmarks/tau3-bank-dev-local-native-safe.toml`. Set both the
declared run configuration and the separately started sidecar to the same format:

```toml
agent_proposal_format = "native_tools_v1"
```

```bash
export SAFE_EPISODE_BACKEND=local_openai
export SAFE_EPISODE_PROPOSAL_FORMAT=native_tools_v1
```

The endpoint, model alias, private service directory, dispatch key and explicit
local timing must also match the existing local setup documented in
`TAU3_LOCAL_RELIABILITY.md`. The sidecar uses only a numeric loopback origin and
never reads a hosted provider key on this route. Native function support in the
chosen local server and model template must be verified as part of the serving
condition; the model alias alone does not attest weights or hardware.

## Evaluation sequence

Freeze the source, weight and server hashes, template, serving arguments,
hardware limits, model settings and diagnostic sequence before inference.
Use fresh private state and preserve stopped and unsuccessful runs.

1. Run the authored discovery pack with the legacy and native formats under the
   same model, runtime, questions, seed, deadlines and hardware. A hardware change
   requires a fresh control; earlier timings are not a matched comparison.
2. Validate the unchanged official customer interface separately. An agent-side
   tool adapter does not repair customer role drift.
3. Run the fixed confirmation pack only for a condition supported by discovery
   and customer evidence. Do not tune on its results.
4. Require a fresh fixed 24-probe qualification, exact trace review and matching
   report/review files before official dispatch. Diagnostic files cannot qualify
   a launch. Retain the existing stop rules and semantic review requirement.

Do not label authored pass counts as τ³ scores or estimates of professional
success. A proposal, admitted read, grounded answer and graded task are distinct
outcomes. Record each separately.

Development exposure from earlier work includes executed tasks 001–005, inspected
task 074, and a broad search that visibly returned part of task 045. The broad
search was truncated, so the full exposure set is unknown. This development
workflow cannot support a blind or uncontaminated holdout claim.

## Validation scope

Offline tests exercise the actual native HTTP response adapter, strict parsing,
data preservation, shared deadline path, existing receipt guard, explicit route
selection, capability identity, durable run binding and qualification mismatch
rejection. They are authored fixtures and do not measure model quality.

Live results and the exact tested serving conditions belong in the associated
development evidence report. A passing code test suite alone never authorizes
an official local launch.

For bounded phase observations and small authored latency cases, see
[Local latency diagnostics](TAU3_LOCAL_LATENCY.md).

For the separate `native_tools_v2` experiment that removes the duplicated
message-function manifest and places stable context first, see
[Native proposal prefix experiment](TAU3_NATIVE_PREFIX.md). V1 remains available
with its original rendering; v2 does not inherit v1 qualification.

## Development observations on 2026-10-28

The initial interface implementation at `b49daa6c1ae5387ddd9f704cebdee8df7f7ad014`
was frozen before the following local diagnostics. The serving condition used
CPU-only inference with an eight-CPU quota, 8 GiB of memory, a 32,768-token
context, a 2,048-token output cap, seed 300, temperature zero, and no retries.
The provider, transport and request deadlines remained 90, 105 and 115 seconds.
No hosted inference or official benchmark task/evaluator ran.

| Condition | Recorded observation | Consequence |
| --- | --- | --- |
| Qwen2.5-3B, legacy JSON discovery | The process stopped, but a workspace interruption removed its report and traces. Exact requests and reason are unknown. | Incomplete control evidence; no matched interface comparison. |
| Qwen2.5-3B, native discovery | The first case timed out after 90.098 seconds without a usable reply; three cases were unrun. | No tool-selection verdict; customer and later stages blocked. |
| Granite 4.0 Micro, official customer interface | Three text responses completed; the fourth native-tool request timed out after 90.544 seconds. | Agent discovery and later stages blocked. |

Semantic review found that all three completed Granite replies met their narrow
authored obligations. The v3 automatic checker had falsely rejected the polite
refusal; the separate review preserves that raw result. This finding motivated
qualification profile v4 and regression tests, not a retroactive qualification.
Token usage for both timed-out requests remains unknown. The recorded
continuation contains three completed and two uncertain requests, excluding the
lost control whose request count is unknown.

Neither condition qualified. Confirmation, the 24-probe qualification, and the
official pilot remained unrun. These observations support work on latency and
native tool completion under the fixed resource budget; they do not provide a
capability score or establish that the new interface improves model quality.
