# Local latency diagnostics

The local native interface can time out before returning a usable proposal.
A timeout alone does not distinguish prompt processing, generation, sampling,
or response handling. These authored diagnostics retain bounded observations
without changing the official customer simulator or evaluator, model request
fields, trusted execution boundary, or qualification gate.

## Opt-in slot observation

Add `--observe-local-llama` to `scripts/run_tau3_authored_diagnostics.py` when the
declared numeric loopback endpoint is a compatible llama.cpp server:

```bash
python scripts/run_tau3_authored_diagnostics.py \
  --config configs/benchmarks/tau3-bank-dev-local-native-safe.toml \
  --tau-root ../tau2-bench-v1.0.1 \
  --output-dir artifacts/tau3/authored-latency-example \
  --pack customer-interface --max-seconds 600 --observe-local-llama
```

The model server and SAFE sidecar must already be running under the matching
local configuration. The output directory must be new. Default diagnostics do
not query the slot endpoint.

The observer performs only `GET /slots` on the same numeric loopback origin,
without proxies, redirects, credentials or retries. Each observation has an
absolute deadline. One worker polls at bounded intervals, streams a bounded
response, writes a private JSONL record, and stops with the enclosing diagnostic.
Response sizes and total samples are capped. It never sends a completion request,
executes a tool, changes server settings or changes the diagnostic's model result.

`local-slots.jsonl` retains a small allowlist of counters, task identities and
selected serving settings. It excludes prompt text, generated text, headers and
raw error strings. Missing fields and negative sentinels are unavailable values,
not zeros. Observation errors use fixed codes. The final report includes a
separate observation summary; a start, polling or close failure cannot turn an
uncertain inference into a completed result or change model request accounting.

Progress output uses `tool_calls: null` when a probe does not expose that summary.
It must not imply zero calls from an absent field. Customer-native proposals are
retained in the private trace, whose digest binds the text and proposed arguments;
the ordinary-text digest alone does not bind a function proposal.

## Small authored packs

The existing discovery, confirmation and customer-interface packs remain
available. Two smaller packs isolate already authored cases:

| Pack | Scope |
| --- | --- |
| `customer-native` | One unchanged official customer-simulator request to propose `qualification_add(a=2, b=3)`. The diagnostic does not execute the proposed tool. |
| `agent-latency` | The existing retained-fees preference case, with at most one SAFE model attempt and no required read. A tool proposal can exhaust this intentionally small budget; this is not an end-to-end task test. |

The stricter one-attempt cap never increases a smaller configured limit. Every
pack remains `diagnostic_only`, has `eligible_for_qualification=false`, and has
no capability score. An observed successful proposal cannot replace discovery,
confirmation, the fixed 24-probe qualification, or attributed semantic review.

## Interpret the evidence conservatively

The pinned llama.cpp b11936 slot response exposes processed and cached prompt
counters and a generated-token counter. Its `n_prompt_tokens` field is the
current slot buffer size, which can grow during generation; it is not the
immutable request input-token count. Confirm the input using the server's
`/apply-template` and `/tokenize` metadata endpoints on the exact request payload.
Those routes do not submit a completion request.

Slot reads share the server task queue. A slow or timed-out observation can mean
that a model batch has not yielded; it does not prove that progress stopped.
Polling also allocates server task IDs. Gaps between task IDs therefore cannot
be used to count model requests. Use the actual inference records and server
completion-launch events, keeping observer GETs separate.

Partial counters and server logs are diagnostic observations, not complete
provider-reported usage or independent execution attestations. In particular,
do not refund reservations, reconcile uncertain attempts, replay pending work,
or fill missing final usage from a slot counter. Existing uncertainty and
recovery controls remain authoritative.

For an explicitly private, authored-only managed investigation, the server's
`LLAMA_SERVER_SLOTS_DEBUG=1` and timestamped verbosity 5 can retain additional
prompt/token details in its own logs. These are separate from the observer's
content-free output. Keep such logs private, record the observability changes,
and account for their overhead when comparing timing conditions.

## Investigation on 2026-10-28

The initial investigation continued from PR49 head `5fd8457`. The isolated
runtime, exact model weights and pinned server were checked before execution.
The host still had an eight-CPU quota and 8 GiB memory, but its CPU quota period
changed from `800000 100000` to `1600000 200000`; both represent eight CPUs.
Earlier timings are therefore context, not a matched new control.

Before any new completion request, the pinned server measured these rendered
inputs through its template and tokenizer endpoints:

| Input | Tokens |
| --- | ---: |
| Granite customer preference | 314 |
| Granite customer account-opening question | 313 |
| Granite customer refusal | 312 |
| Granite customer native-tool request | 812 |
| Qwen native SAFE retained-preference request | 5,461 |
| Qwen counterfactual with duplicate message schema replaced by its name | 5,294 |

The customer payloads were reconstructed through the unchanged frozen SDK path;
the first three counts match the prior completed responses. The native agent
payload was reconstructed from its durable request. Original timeout wire
captures were unavailable, so this does not retroactively supply missing usage.

Removing the duplicate message schema would save 167 input tokens, about 3% of
the Qwen prompt. That counterfactual was measured without generation and was not
deployed. Domain schemas, policy, conversation, consent and receipt data were not
removed. Stable-prefix ordering is a possible later-request cache optimization;
it cannot accelerate an empty-cache first request.

A new predeclared campaign permits at most ten completion requests: one
instrumented Granite control pack, an optional one-request Qwen control, and at
most one evidence-selected remedy under the same deadlines/output limits. Each
pack stops at its first uncertain request. The campaign does not include an
official task or evaluator. Its exact inputs, serving/source hashes, logs,
outcomes and cleanup records belong in the accompanying development evidence.

### Recorded campaign outcome

The campaign closed at its ten-request cap: eight completed customer requests
and two uncertain agent requests. Every inference used the frozen investigation
source and qualification v4. All owned services were stopped; the two Qwen
servers required forced termination after the bounded graceful-cleanup interval.
Neither uncertain attempt was retried or reconciled from partial counters.

| Condition | Recorded result | Interpretation |
| --- | --- | --- |
| Granite, F16 cache | Four customer requests completed; native proposal in 22.281 s. | The expected `qualification_add(a=2, b=3)` proposal was parsed; no tool was executed. The older native timeout did not recur. |
| Qwen, F16 cache | One request uncertain after 90.113 s; 4,096 prompt tokens cumulatively processed at the last observation, zero generated. | Cold prompt processing exceeds the deadline in this condition; no tool-selection verdict is possible. |
| Granite, Q8_0 K/V cache | Four customer requests completed; native proposal in 28.345 s. One clear refusal failed the finite v4 checker. | Cache memory fell, but timings did not improve consistently. Separate semantic review confirmed the refusal. |
| Qwen, Q8_0 K/V cache | One request uncertain after 90.137 s; 2,048 prompt tokens cumulatively processed at the last observation, zero generated. | The single memory mitigation did not resolve the prompt-processing timeout. |

The completed requests reported 3,502 input and 268 output tokens in total.
Final provider usage for both uncertain requests remains unknown; partial slot
counters are not a substitute. The four observer runs retained 69 samples, all
successful, with no completion requests attributable to observation.

The Qwen control and candidate inputs were 5,463 and 5,456 tokens respectively.
Fresh opaque references and observation hashes vary between runs. The authored
conversation, policy, schemas and request settings remained the same, but the
serialized inputs were not byte-identical. The earlier 5,461-token measurement
was from a separate preserved request, not either live input.

The Granite control's native request evaluated 805 new tokens and reused seven:
16.612 s for prompt processing and 5.613 s for generation. Its Q8 counterpart
took 21.878 s and 6.418 s. These observations identify phases; a single run per
condition does not establish a reliable latency distribution or causal speedup.
The three F16 text replies matched the earlier completed replies exactly.

### Single memory mitigation

The one predeclared serving change added only:

```text
--cache-type-k q8_0 --cache-type-v q8_0
```

The pinned server accepted both cache types with the existing Flash Attention
mode. Context size, model weights, embedded templates, CPU quota, memory limit,
authored prompt content, output cap, seed, temperature, deadlines and zero retries
were retained.
Quantized attention caches change inference numerics and can change output even
with the same weights. The default serving condition was not changed.

| Model | F16 cache allocation | Q8_0 allocation | Reduction |
| --- | ---: | ---: | ---: |
| Granite 4.0 Micro | 2,560 MiB | 1,360 MiB | 1,200 MiB |
| Qwen2.5-3B | 1,152 MiB | 612 MiB | 540 MiB |

These are measured cache allocations, not total-process or cgroup savings.
Shared cgroup peaks include file cache and other processes. The Granite control
reached the 8 GiB limit; that alone cannot establish a reclaim stall. Aggregate
memory-event/stat counters were captured around the candidates only, so missing
control counters cannot be treated as zeros or used for a matched causal claim.

The generic diagnostic `condition_sha256` does not include server cache flags.
The separate managed `condition.json`, exact server command, model-metadata
digest and allocation logs distinguish the F16 and Q8 conditions in the evidence.
The generic report digest or model alias alone is insufficient serving identity.
These records are observations, not independent model/server attestations.

### Follow-up corrections and remaining gate

The Q8 customer declined changes with “I'm not comfortable with making any
changes to my account details at this time.” The raw v4 rejection is preserved.
Qualification v5 adds a bounded, account-scoped discomfort-refusal pattern and
adversarial regressions, retaining the affirmative-permission veto. This is a
diagnostic correction; runtime exact-action consent remains separate. Earlier
v4 reports cannot satisfy v5, and no live result here was rerun as v5 evidence.

The implementation also corrects the progress-line ambiguity described above.
Those post-campaign source changes were validated separately from the frozen
live condition. The memory option remains experimental; Qwen's prompt-processing
deadline is unresolved. No discovery/confirmation expansion, 24-probe
qualification, official task, evaluator or benchmark score follows from this
campaign. A future launch requires fresh qualification and attributed review,
with the complete serving condition recorded explicitly.

The [development evidence bundle](https://chatgpt.com/library?file_id=libfile_32e626206e6881918a2852f3cd30c3de&account_id=personal)
contains the frozen plan, source/serving identities, raw reports and traces,
phase/resource observations, independent audits and validation records.

A separate continuation investigates a versioned presentation with less
duplicate schema text and a longer stable prefix. See
[Native proposal prefix experiment](TAU3_NATIVE_PREFIX.md). The ten-request
campaign above remains closed; its uncertain attempts and reports are unchanged.

Pinned implementation references:

- [Server endpoints and options](https://github.com/ggml-org/llama.cpp/blob/b11936/tools/server/README.md)
- [Slot counters, queue handling and cancellation](https://github.com/ggml-org/llama.cpp/blob/b11936/tools/server/server-context.cpp)
- [Native chat and template processing](https://github.com/ggml-org/llama.cpp/blob/b11936/common/chat.cpp)
