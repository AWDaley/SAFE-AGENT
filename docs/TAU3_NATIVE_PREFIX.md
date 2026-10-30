# Native proposal prefix experiment

`native_tools_v2` is an opt-in local agent presentation condition for reducing
repeated prompt content. It preserves the original native interface as
`native_tools_v1` and leaves `candidate_json_v1` as the default.

The preceding latency investigation found that Qwen's first native request
reached its provider deadline during prompt processing. Most of that prompt
was required policy and the supplied tool catalogue. V2 removes one duplicate
synthetic function definition and orders stable context fields before changing
dialogue and state. The ordering is intended to make subsequent requests more
cacheable; it does not eliminate a cold request's policy or tool input.

## Exactly what changes

Both native versions provide the same domain functions, descriptions, parameter
schemas and synthetic `safe_agent_message` function in the native tools array.
V1 also repeats the complete message function in its system protocol manifest.
V2 replaces that repetition with the function's fixed name. The natural-language
system instructions remain unchanged.

V2 serializes these user-context fields first, when present:

1. `domain`
2. `domain_policy`
3. `privacy_profile`
4. `completion_profile`
5. `native_tool_metadata`

All other fields follow in sorted order. Nested values retain the existing
finite, canonical JSON encoding. No field is dropped, summarized or shortened;
unknown future fields are retained. Decoding either version's user message
produces the same object. Native function order and all tool metadata are
preserved.

This is a dedicated presentation serializer. It does not change `wire_json`,
request digests, authenticated protocol bytes, durable evidence, or signatures.
Policy and historical content remain in the user context. Historical customer
text and tool results gain no system authority through reordering.

Identical decoded data does not guarantee identical model behavior. The new
manifest and ordering constitute a new experimental condition and require
their own diagnostics and qualification.

## Identity and execution boundaries

The sidecar advertises `local_openai_tools_v2`, with a distinct system instruction
hash and an explicit `message_function_hash` over the complete synthetic function
definition, including its description and schema. This field is required only
for v2 and omitted from older capabilities. The candidate-schema hash continues
to bind the unchanged SAFE candidate union. Capability authentication rejects
v1/v2 mismatches and mismatched message-function definitions.

The selected proposal format remains bound to configuration, durable run identity,
saved provenance and qualification conditions. An existing run cannot resume
under another version, and qualification evidence cannot be borrowed between
versions. Hosted inference, custom runtime routes and stock-agent routes cannot
silently select this local SAFE presentation.

V2 uses the same native response parser, one-function requirement, argument and
response limits, absolute deadlines, zero-retry transport, and uncertainty
accounting. A function call remains an untrusted proposal. Only the trusted host
can resolve opaque references, check policy and exact-action consent, authorize
an action and correlate its result. A generated provider call ID is not a host
receipt. Successful-mutation messages still require a real successful receipt.

## Selecting v2

Use `configs/benchmarks/tau3-bank-dev-local-native-v2-safe.toml` as a separate
development condition and select the matching sidecar format:

```toml
agent_proposal_format = "native_tools_v2"
```

```bash
export SAFE_EPISODE_BACKEND=local_openai
export SAFE_EPISODE_PROPOSAL_FORMAT=native_tools_v2
```

The endpoint, model, timing, private service directory and dispatch key must
match the rest of the local configuration. The new template retains the v1
model, context/runtime budgets, output cap, zero retries and 90/105/115-second
provider/transport/request deadlines. It is not a qualified launch configuration
merely because the files exist or the code tests pass.

## Measurement boundaries

Exact template/token measurements can compare two rendered requests without
submitting a completion. A longer common token prefix demonstrates an
opportunity for reuse. Actual reuse must be observed in the serving process;
it depends on cache state, template, tools, policy and scheduling. Reordering
cannot create a cache hit on an empty slot.

A cold/warm diagnostic must use fresh private episodes, count every actual
model request, retain failures, and stop on uncertain outcomes. A warm request
must not replay a pending uncertain operation. Partial prompt counters cannot
replace final provider usage or release a conservative reservation.

The current development study uses a fresh AMD EPYC host rather than the Intel
host from the preceding campaign. Equal CPU quotas and model hashes do not make
those historical timings a matched comparison. Current v1 and v2 controls use
the same host and explicit F16 serving settings. Single observations still do
not establish a latency distribution, reliability or professional readiness.

The fixed discovery/confirmation sequence, 24-probe qualification and attributed
semantic review remain required before an official local launch. No authored
prefix diagnostic is an official τ³ task or capability score.

## Development observations on 2026-10-30

The live study used frozen tree `11aaeb47fbee2e238f93936ad8858edb0e713a47`,
based on PR50 head `d91a9bf`. It retained the pinned Qwen2.5-3B Q4_K_M weights,
llama.cpp b11936, 32,768-token context, eight threads, F16 K/V cache, Flash
Attention, 2,048/512 logical/physical batches, 2,048-token output cap, temperature
zero, seed 300 and the existing 90/105/115-second deadlines. The current host
reported AMD EPYC 9V74, CPU quota `800000 100000` and 8 GiB memory.

### Exact metadata measurements

The two preserved requests from the preceding Qwen campaign were independently
rendered in both versions through the pinned server's template/tokenizer routes.
Their original uncertain outcomes were not replayed or reconciled. Every tool
definition, decoded context field and system-prose character matched between
versions; the v1 golden regressions also matched the published pre-change bytes.

| Preserved input | V1 tokens | V2 tokens | Reduction |
| --- | ---: | ---: | ---: |
| Prior F16 request | 5,463 | 5,289 | 174 |
| Prior Q8 request | 5,456 | 5,282 | 174 |

The reduction is about 3.2%. Across that pair, the common rendered token prefix
increased from 3,392 to 4,743 tokens. The common user-context byte prefix
increased from 492 to 7,595 bytes, while each full user-context string remained
9,024 bytes. These are exact static comparisons, not observed cache hits.
Two metadata-only sessions produced identical measurements and zero generation
requests.

### Bounded live result

An initial setup attempt failed before inference because its private Unix socket
path exceeded the operating-system limit. Its server and sidecar were stopped;
zero diagnostics or completion requests ran. A separate recovery declaration
shortened the path, verified the zero-request predecessor and preserved the same
source, serving conditions and aggregate four-request maximum. The failed setup
and both declarations remain in the evidence.

The recovery study closed after two requests. Each condition began with a fresh
server and empty KV state. File/page cache was not independently controlled.
A second independent episode was allowed only after a complete, issue-free
first diagnostic. Both conditions failed that requirement, so neither warm
request ran.

| Condition | Request result | Provider input/output tokens | Request time | Recorded action |
| --- | --- | ---: | ---: | --- |
| V1, cold KV | COMPLETE | 5,459 / 24 | 58.240 s | Proposed and executed `KB_search` for the already supplied preference; no answer. |
| V2, cold KV | COMPLETE | 5,280 / 27 | 57.439 s | Proposed and executed `KB_search` for the already supplied preference; no answer. |

The question asked the agent to repeat the preference already present in its
history. Both versions spent their one permitted model attempt on an unnecessary
read. The durable records retain one authorized call and one successful result
per condition, but no customer-facing answer or second model attempt. Both
episodes finished as failed. A completed model request and successful read do
not make the authored task successful.
The one-attempt latency diagnostic intentionally cannot observe an answer after
a read; it does not measure whether a longer conversation would recover.

The server recorded zero cached input tokens in both conditions. V1 spent
55.613 seconds processing its prompt and 2.534 seconds generating; v2 spent
55.231 and 2.124 seconds respectively. The approximately 0.8-second request-time
difference in this single pair is not a demonstrated speedup. Fresh opaque
identifiers and hashes also make the two live inputs differ; the exact same-input
token reduction comes from the metadata comparison above.

All final provider usage was available for these two requests: 10,739 input and
51 output tokens. All 24 observer samples succeeded. Both model servers and
sidecars stopped without forced termination. The preceding campaign's uncertain
attempts remain unchanged. No warm-cache benefit, model reliability, official
task result or qualification was established here.

### Partial-trace reporting correction

The live failure exposed an existing diagnostic bug. When the orchestrator raised
after recording a tool call/result, `_run_agent_probe` discarded that partial
trajectory and reported empty tool arrays. The SQLite records preserved the
actual actions. The original live reports and short error traces remain intact;
the independent audit uses their durable records to explain the discrepancy.

A post-campaign reporting correction now retains the pinned orchestrator's
recorded partial trajectory on failure and labels it partial. If the trajectory
cannot be obtained, its messages, tool summaries and answer digest are explicitly
unavailable. A secondary cleanup error cannot erase an already captured trace.
The original execution failure remains a failure, and no extra model request,
receipt, tool result, usage or answer is synthesized. Offline regressions validate
this correction separately from the frozen live source.

The remaining observed model problem is selecting an appropriate next action
for information already in context. V2 remains opt-in. Further model/prompt
selection requires another predeclared study and fresh qualification; this
closed comparison is not extended to tune against its results.

The [development evidence bundle](https://chatgpt.com/library?file_id=libfile_b2dbb0918fa48191b8cdbd9affa413c0&account_id=personal)
retains both setup declarations, frozen source and serving identities, exact
metadata comparisons, raw reports and journals, independent reviews, cleanup,
and separate implementation/CI validation.
