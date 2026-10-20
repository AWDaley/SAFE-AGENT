# Benchmark domain packs

`safe_agent.domains` is additive to the existing office implementation. It is
scoped to the frozen tau v1.0.1 public interfaces at commit
`fc0055dc4e0a316c3f83133267fbd6faaa770992`.

## Runtime API

```python
pack = get_domain_pack("retail", tool_schemas=visible_tool_specs)
context = pack.observe_user(text, context, user_turn_id="actual_user_turn_1")
decision = pack.admit(proposal, context)
# The trusted episode runtime commits and dispatches separately.
context = pack.observe(proposal, actual_correlated_result, context, success=True)
```

`ToolProposal` is a strict domain, tool-name, and JSON-argument contract. The model
does not classify effects or supply authorization/verification flags.
`DomainContext` is protected host state, never a model output contract. The host
updates its durable observation hash/version before admission and correlates
every tool result with the actual outstanding call. `observe` increments the
observed call count; the host may replace this with its durable dispatch count.

Retail admission covers verified customer lookup, one customer per episode,
observed order ownership, pending/delivered status, cancellation reasons, item
counts, known available replacements of the same product, payment ownership,
return refund destination, and observed gift-card balance. Every retail business
write requires action-bound customer confirmation. `CUSTOMER_CONSENT_REQUIRED`
is returned only after the other mechanical checks pass. Observing a new user
turn clears old consent. The trusted runtime must bind a new `CustomerConsent`
to the exact displayed action and actual confirming user turn.

Banking lookup identifies a subject but does not authenticate it. Verification
requires a successful observed `log_verification`, matching observed customer
record fields, at least two identity values present in actual user input, and a
timestamp supplied by `get_current_time`. Subsequent customer operations require
authentication and observed ownership of referenced accounts, cards, and
transactions. Identity matching is conservative: case and whitespace are
normalized; ambiguous or differently formatted identity values may require
clarification.

Banking discovery is distinct from execution. A tool name must occur in an
actual retrieval result, agent tools must have a successful observed unlock,
and dispatcher arguments are validated against the private pinned signature.
Nested arguments reject duplicates, unknown fields, nonfinite values, and type
mismatches. User-only tools can be handed to the user with validated partial
arguments; the agent cannot execute them. The wrapper is classified as a write
for retry purposes even if its inner operation reads, because the official
wrapper can record the call in benchmark state. The hidden evaluator's read-log
allowlist is never consulted.

The checked-in private catalog contains only public tool signatures/decorators.
Regenerate it with `scripts/generate_tau_domain_catalog.py /path/to/pinned/tau`;
`--check` verifies reproducibility. The generator reads exactly three named
public Python sources. It never imports tau or reads tasks, databases, or gold
trajectories. Only the supplied visible top-level ToolSpecs are exposed by a
pack's `capabilities` property. Shell retrieval is not supported by these packs;
the initial benchmark profile uses BM25.

## TAL and scope of assurance

Default pack creation requires the separate reviewed benchmark TAL VCS install,
module origins, and module hashes via `safe_agent.tal.domain_runtime`. There is
no environment bypass or editable-install exception. Office TAL remains pinned
independently. Authored unit fixtures explicitly inject the pure
`tal.domain_admission.admit_domain_action` evaluator. Production factories must
not expose that injection option through model input or run configuration.

Admission is not execution authority. The episode store/coordinator owns exact
call commitment, state-version binding, dispatch, budgets, response privacy, and
recovery. Raw customer/tool observations in a context are sensitive private
state; they must not be placed in an audit/public report or treated as safe
customer-visible response text.

Every domain decision sets `semantic_policy_verified=False`. Banking knowledge
base eligibility, intent, fairness, and response disclosure rules still require
semantic reasoning and official evaluation. These packs do not implement every
policy condition, and authored/offline tests do not establish hosted-model task
success. A benchmark result must separately report utility, policy violations,
execution failures, costs, and every attempted/scored task.
