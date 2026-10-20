# SAFE-AGENT implementation plan for τ³-bench

**Prepared:** October 20, 2026  
**Status:** Implementation plan; no benchmark runs or implementation changes have been performed for this document.  
**Primary goal:** Run and independently score SAFE-AGENT on every task in the frozen τ³ banking text domain, then compare it with the stock agent using the same underlying model.

## 1. Recommended scope and version decision

Build a reusable conversation-and-action runtime inside SAFE-AGENT, connect it to the official τ benchmark, prove a minimal retail integration, and then support the banking knowledge domain. Complete the wider retail implementation in parallel with banking work. Preserve SAFE's deterministic authorization, scoped references, budget admission, and evidence trail throughout the evaluated path.

The first research endpoint is **English text, retail and banking_knowledge, on τ³ v1.0.1**. Retail is the integration milestone. A complete, reproducible banking comparison is the principal deliverable. Initial banking retrieval uses the official **bm25** configuration.

Upstream released τ-bench 2.0 around the time of this planning review. It changes inputs, grading, and released benchmark definitions; its results are not comparable with 1.x. The current documentation directs users seeking 1.x comparability to v1.0.1 and says 1.x submissions remain accepted. Consequently, this plan deliberately pins the τ³ release rather than following main. [S1]

| Component | Inspected or selected revision |
| --- | --- |
| SAFE-AGENT baseline | b9ab733088d54c92a6608f90c90418592d0b3ddc |
| τ³ benchmark release | v1.0.1 |
| Resolved τ³ commit | fc0055dc4e0a316c3f83133267fbd6faaa770992 |
| Existing SAFE TAL pin | e9d09ef47c0e0e18f3955f9c58f7a8941baf6c38 |
| Newer upstream main observed during review | 989d3285ae669e49503324bdb9981bf14fccedeb — 2.0.0; outside the initial experiment |

The existing TAL pin is recorded in SAFE's ci/tal-office-commit.sha. The v1.0.1 release includes banking grading fixes, so earlier banking scores must also be kept separate. [S2] [S3]

### Completion criteria

The project is complete when:

1. Every declared banking task can enter the actual SAFE runtime, produce a terminal trajectory, and be processed by the unchanged pinned evaluator.
2. The implementation supports the domain's conversation, retrieval, discovery, identity, mutation, user-guidance, and handoff mechanisms.
3. SAFE and the stock baseline have a reproducible comparison using matched models, simulator, task set, retrieval configuration, and trial schedule.
4. Unsupported behavior, model failures, policy denials, interruptions, costs, and uncertainty are visible in the report.
5. The existing office/calendar workflow and its historical records remain compatible.

This definition does not promise that every task succeeds or that SAFE beats the baseline. Those are measured outcomes. A valid low score is a completed experiment; omitted tasks, concealed failures, or a bypassed SAFE runtime are not.

### Deferred scope

Airline, telecom, voice, multilingual support, τ-bench 2.0 migration, AgentDojo, and an AllTools retrieval comparison are separate extensions. Retail plus banking should be described by those domain names, not as completion of every τ³ track.

No live banking or customer-system integration is required. The benchmark operates on its own simulated applications.

## 2. Current implementation and the actual gaps

The current code has useful reliability foundations, but its planning and execution vocabulary is specialized to a calendar task. This is an architectural extension, not a model-key configuration change. [S4] [S5] [S6] [S7] [S8] [S9]

| Existing component | Reuse | Required extension |
| --- | --- | --- |
| Strict contracts and canonical hashing | Closed schemas, immutable bindings, stable action hashes | Versioned episode, turn, domain action, and response contracts |
| Calendar planner | Proposal-first separation from execution | Read/query, retrieval, clarification, confirmation, grounded response, handoff, and multi-action planning |
| Coordinated inference | Admission before dispatch, cost holds, durable validated results, conservative uncertainty handling | Episode-aware input/output schemas and a real model backend |
| Planning storage | Immutable input and result-consumption evidence | New episode/turn records; current planning input is unique per task |
| Office ledger | Single-effect authorization and verification | Separate episode completion from individual action completion |
| Reference broker | Scope, actor, operation, resource kind, expiry and revocation | Retail/banking resources, read and write permissions, dynamic capabilities |
| Virtual office | Observed projections and deterministic preview patterns | Domain projections built only from permitted benchmark observations |
| Approval service | Exact-action binding | A benchmark customer-consent contract distinct from independent operator approval |
| TAL integration | Deterministic calendar authorization and dispatch patterns | Explicit retail/banking domain contracts and conformance |
| Outbound responses | Safe reason codes and fixed clarification | Useful typed questions, policy answers, exact confirmations, and outcome-backed summaries |

Concrete blockers:

- InferenceRequest currently binds SafeTaskContext and a calendar Projection, and its operation class is calendar.propose_or_clarify.v1. [S4]
- The supported inference service admits the exact OfflineMockInferenceTransport implementation. A real backend requires an explicit new path. [S5]
- Coordinated planning allows one immutable input per task. [S6]
- ACTION_VERIFIED moves the current office task to COMPLETED after one effect. [S7]
- Broker operations and TAL dispatch contracts are calendar/internal-task specific. [S8] [S9]
- The current gateway replaces model-generated clarification text with a fixed sentence. General customer conversation therefore needs a new response boundary. [S5]

Preserve these v1 assumptions. Do not generalize by removing the unique constraint, replacing a closed operation union with an arbitrary string, changing an old terminal state back to running, or labelling a real provider as a mock.

## 3. Target architecture

### 3.1 One conversation episode, many individually authorized steps

Introduce an Episode above the existing action lifecycle. A turn can observe information, ask a question, obtain confirmation, propose a tool call, receive a result, or finish the conversation. Completing one action does not finish the episode.

Suggested episode states:

- READY
- INFERENCE_PENDING
- AWAITING_CUSTOMER
- TOOL_PENDING
- OUTCOME_UNCERTAIN
- HANDOFF
- FINISHED
- FAILED

Keep action states separate: PROPOSED, DENIED, AUTHORIZED, PENDING_RESULT, CONFIRMED, FAILED, and NEEDS_RECONCILIATION. PENDING_RESULT records a prepared/emitted call whose execution outcome is not yet established. A bounded inference-format repair is an inference attempt; it is not permission to repeat a business action.

Every step binds the run, episode, turn, policy revision, observation revision, candidate hash, and applicable consent. Episode and run budgets aggregate across all steps and survive restarts.

### 3.2 Keep the official orchestrator in charge of tool execution

~~~mermaid
flowchart TD
    T["Official τ runner: customer and tools"] --> A["SAFE benchmark adapter"]
    A --> C["Scoped observations and episode state"]
    C --> M["Isolated model planner"]
    M --> G["Schema, TAL policy and consent checks"]
    G --> R["Authorized call or trusted response"]
    R --> J["Durable journal and budget ledger"]
    J --> A
    A -->|Assistant message| T
    T --> E["Official evaluator"]
~~~

The adapter emits an AssistantMessage containing either a permitted tool call or a user response. The upstream orchestrator executes the tool and returns a ToolMessage. SAFE correlates that result before deciding the next step. This follows the supported HalfDuplexAgent interface. [S10]

Before returning a tool-call message, durably commit its exact arguments, call ID, policy/consent bindings, and pending status. Emitting a message is not proof that the orchestrator executed its tool. An interruption between persistence, emission, and result receipt follows the uncertainty/interrupted-trial policy; it must not cause the call to be silently regenerated.

**Do not execute a tool inside SAFE's preview and then return the same call for τ to execute again.** Do not replace the benchmark environment or evaluator with SAFE's virtual office.

SAFE's internal simulation is a forecast over allowed observations. It can check known preconditions and expected effects, but it cannot read the benchmark's complete database, future user actions, or expected final state. The official environment remains authoritative.

### 3.3 Closed proposal types

Add versioned types for these intentions:

| Type | Required binding |
| --- | --- |
| ReadRequest | Known operation, typed arguments, permitted resource scope |
| RetrievalRequest | Allowed KB, bounded query, retrieval configuration |
| MutationProposal | Exact target, typed change, current facts, policy checks, applicable consent |
| DiscoverCapability | Observed tool name and supporting retrieved document |
| GrantUserCapability | Discovered user-only operation and legitimate current purpose |
| ClarificationIntent | Requested field or ambiguity, allowed question template |
| ConfirmationIntent | Exact pending action digest and disclosed details |
| AnswerIntent | Allowed facts, evidence references, recipient, and response constraints |
| HandoffIntent | Applicable policy condition and permitted escalation |
| FinishIntent | Recorded disposition and established outcomes |

These are proposed types, not existing SAFE APIs.

Use strict operation schemas and discriminators. A generic outer tool-call envelope must never authorize arbitrary inner arguments. The trusted host owns executable registries, resource resolution, and credentials. The planner receives descriptions of allowed proposals.

### 3.4 TAL and domain policy

Add a versioned domain admission contract and retail/banking policy implementations. Keep the current calendar contract and pinned behavior intact.

In this plan, TAL remains mandatory for the new domain authorization path. The work includes a focused TAL change followed by a new reviewed pin in SAFE. Do not route new operations around the existing TAL boundary or claim the calendar evaluator authorizes banking actions.

The new contract should bind:

- Domain, policy bundle version/hash, episode, and action.
- Operation schema and referenced observations.
- Authenticated subject and resource ownership.
- Effect classification, limits, and required consent.
- Deterministic rule results and a precise denial reason.
- The exact action permitted for dispatch.

Do not build a general English-to-policy compiler for this milestone. Encode explicit domain rules with tests. Keep three claims distinct: mechanically valid proposals, compliance with encoded rules, and semantic interpretation of retrieved instructions. Retrieved evidence and a model critic do not prove that every business-policy interpretation is correct.

Represent legitimate KB precedence and override rules in the frozen, source-bound domain policy bundle. Retrieved prose supplies evidence and task guidance; it cannot rewrite the bundle or mint execution authority. An unencoded semantic condition remains an explicit reasoning limitation or conservative denial, not an inferred trusted rule.

### 3.5 Consent, privacy, and response rendering

A benchmark profile grants authority only within the simulated benchmark environment. Customer consent comes from an actual customer message bound to a displayed action. Existing office operator approval remains its own mechanism.

Changing the amount, item set, recipient, address, account, or other material parameter invalidates prior consent. Ambiguous consent triggers clarification. Checking the source and action binding is deterministic; interpreting arbitrary natural-language intent is not universally deterministic.

Keep sensitive identity fields and raw resource IDs in trusted storage. The planner uses scoped references, permitted facts, and verification status. Resolve private values only when producing an authorized tool argument or recipient-authorized response.

Define a field-level exposure policy. Avoid blanket masking of product attributes, policy terms, dates, or amounts that the task requires. Where a value must remain private, perform the needed calculation or comparison in a trusted helper over references.

Cover user input, tool output, restored history, inference payloads, traces, errors, and final messages. Do not rely solely on a regex scrubber or opaque tool arguments. State the fields and channels actually protected; test them with synthetic canaries. Unrecognized sensitive free text needs an explicit handling policy, not an assumption that it was sanitized.

A completion statement such as “the refund was issued” requires a corresponding successful tool result. Policy explanations may use source-bound paraphrases, but citation presence does not itself establish semantic correctness.

## 4. Delivery sequence

The PR labels below are proposed work packages, not existing GitHub PR numbers. Split a package further if review size warrants it. Proposed paths are implementation targets.

### PR 0 — Freeze the experiment and establish the benchmark workspace

**Purpose:** Make the target reproducible before changing the runtime.

**Changes**

- Add docs/TAU3_IMPLEMENTATION_PLAN.md, configs/benchmarks/tau3-v1.0.1.toml, and a benchmark-specific dependency lock.
- Pin the official benchmark commit and compatible Python 3.12 environment. τ v1.0.1 requires Python >=3.12,<3.14. [S11]
- Record task counts, tool schemas, policy hashes, document hashes, and scoring configuration through an evaluator-side inventory command.
- Define the supported operation inventory, effect classifications, and source references without giving hidden task data to the agent.
- Keep dependencies separate from the accepted coordinated-office runtime lock. Resolve and verify compatibility; use separate environments with a bounded process protocol if a shared lock is unsuitable.
- Add an offline configuration inspection command and budget estimate format.

**Acceptance**

- Inspecting configuration performs no inference.
- The lock resolves to the expected benchmark commit.
- The pinned inventory finds 114 retail tasks and 97 banking tasks. [S12]
- Benchmark fixtures and evaluator data have no path into planner inputs.
- A dependency collision is resolved explicitly, not by forcing incompatible packages into the office profile.

**Dependencies:** None.

### PR 1 — Add episode, turn, and action contracts with durable lineage

**Proposed files**

- src/safe_agent/contracts/episodes.py
- src/safe_agent/contracts/episode_actions.py
- src/safe_agent/evidence/episodes.py
- src/safe_agent/storage/episodes.py
- A new forward migration selected against current migration head

**Changes**

- Implement the episode and action state machines, immutable events, and exact input/result bindings.
- Preserve v1 office task semantics and historical migrations.
- Store one pending authorized action per serial conversation step initially.
- Add episode/run limits for turns, inference calls, tool calls, tokens, wall time, and spend.
- Persist aggregate reservations so additional child actions cannot reset a budget.

**Acceptance**

- One episode performs two sequential actions with an intervening customer turn.
- Reordered, duplicate, stale, and cross-episode records cannot authorize a new effect.
- Restart preserves the exact pending step and budget reservation.
- Existing office records replay unchanged and existing calendar tests pass.

**Dependencies:** PR 0.

### PR 2 — Add domain capabilities, scoped projections, and TAL admission

**Proposed files**

- src/safe_agent/domains/base.py
- src/safe_agent/contracts/domain_policy.py
- src/safe_agent/broker/domain_references.py
- src/safe_agent/tal/domain_dispatch.py
- Versioned TAL domain contracts and corresponding SAFE pin/conformance changes

**Changes**

- Define a DomainAdapter protocol for schema inventory, observations, effect classification, preconditions, consent requirements, preview, and response facts.
- Add private identifier types that correctly round-trip benchmark IDs without exposing them to the planner.
- Scope handles by episode, subject, operation, kind, and lifetime.
- Bind trusted policy checks to current observations; model-supplied “verified” fields confer no authority.
- Extract reusable authorization and journal invariants where practical while retaining the original calendar branch.
- Classify all state changes, including generic discovery/grant operations, rather than inspecting a WRITE label alone.

**Acceptance**

- Wrong-customer, wrong-operation, wrong-kind, expired, revoked, and cross-episode references fail closed.
- Changing policy, target, arguments, or observation revision invalidates an authorization.
- Missing or incompatible mandatory TAL code cannot enable execution.
- Domain simulation uses only observations legitimately available to the agent.

**Dependencies:** PR 1. TAL contract work can proceed alongside SAFE storage work.

### PR 3 — Add episode-aware inference and a real model route

**Proposed files**

- src/safe_agent/contracts/episode_inference.py
- src/safe_agent/inference/episode_gateway.py
- src/safe_agent/inference_service/episode_service.py
- src/safe_agent/inference_service/providers/
- src/safe_agent/storage/episode_inference_admission.py
- src/safe_agent/storage/episode_inference_dispatch.py
- src/safe_agent/storage/episode_inference_results.py
- Versioned coordinated transport/envelope contracts and forward persistence changes
- A dedicated benchmark runtime profile

**Changes**

- Introduce a versioned inference request containing the sanitized conversation state, available proposal schemas, and bounded retrieved evidence.
- Reuse admission, reservation, dispatch fencing, durable result validation, and uncertain-outcome handling.
- Implement episode-aware admission, dispatch envelopes, coordinated transport, and durable request/result storage. Existing calendar task identity checks, exact transport constraints, and stored v1 payload types cannot accept these requests unchanged. Extract shared low-level primitives behind new contracts where useful, preserving the calendar APIs and historical evidence.
- Add one explicitly configured local or hosted model backend. Keep the existing offline mock service explicit.
- Allowlist model routes and credentials through trusted bootstrap; never accept a provider URL or secret from a model proposal.
- Disable invisible SDK retries or account for every retry through the coordinator.
- Add bounded schema repair with separate call accounting.
- Preserve the distinction between provider-reported usage, estimated cost, and independently verified billing.

**Acceptance**

- A scripted backend and then the selected real backend produce the same typed decision interface.
- Malformed JSON, unknown operations, mismatched result hashes, timeouts, and provider errors cannot authorize tools.
- Sanitized errors and logs contain no keys or protected canary values.
- Each attempt is budgeted before dispatch; unresolved dispatch cannot silently trigger another attempt.

**Dependencies:** PR 1; consumes PR 2 schemas.

### PR 4 — Add conversation intake, consent, and trusted responses

**Proposed files**

- src/safe_agent/conversation/intake.py
- src/safe_agent/conversation/consent.py
- src/safe_agent/conversation/renderer.py
- src/safe_agent/contracts/responses.py

**Changes**

- Support identity questions, missing fields, clarification, action summaries, confirmation, policy explanations, user-tool instructions, and handoff.
- Create separate customer-consent evidence bound to the actual displayed action and customer turn.
- Retain private identity evidence outside model context.
- Render exact account/order IDs and other approved values through scoped bindings.
- Bind critical success statements to successful results.
- Preserve policy-specific message requirements and turn-taking.

**Acceptance**

- Missing, negative, stale, conditional, and materially changed consent cannot authorize a mutation.
- A model cannot render another customer's private reference.
- A source document or tool response cannot manufacture customer consent.
- A read → clarify → confirm → mutate → summarize conversation works with scripted inference.
- Ordinary conversational correctness is evaluated separately from mechanical output checks.

**Dependencies:** PR 1 and PR 2; can use scripted inference while PR 3 is completed.

### PR 5 — Connect the actual SAFE runtime to the official τ loop

**Proposed files**

- src/safe_agent/benchmarks/tau3/adapter.py
- src/safe_agent/benchmarks/tau3/runner.py
- src/safe_agent/benchmarks/tau3/messages.py
- src/safe_agent/benchmarks/tau3/__main__.py

**Changes**

- Implement HalfDuplexAgent.get_init_state and generate_next_message.
- Handle UserMessage, ToolMessage, MultiToolMessage, pre-existing message history, and termination.
- Register the SAFE factory and invoke tau2.runner.run_domain with TextRunConfig in the same process.
- Use a SAFE-owned CLI to perform registration. Registering in another process does not modify a later stock tau2 CLI invocation.
- Keep the official user simulator, tools, environment, and evaluator unchanged.
- Explicitly discard privileged factory task metadata before constructing planner-visible state.
- Emit authorized calls for the upstream orchestrator to execute; correlate incoming tool call IDs with the action journal.
- Commit the exact authorized outgoing call, ID, bindings, and pending state before returning it to the orchestrator. A lost handoff acknowledgment cannot regenerate the call.
- Reset episode-local references, consent, identity, and state between independent trials. Allocate each new episode budget under the unchanged aggregate run/global cap; prior costs and uncertain reservations remain charged or held.

**Acceptance**

- A scripted official-harness episode reads data, asks a question, processes an answer, emits one authorized mutation, receives the result, and finishes.
- Every emitted action has matching SAFE authorization and a τ trajectory entry.
- The adapter never directly invokes tool callables, mutates benchmark DBs, or executes user-only tools.
- Hidden task instructions, evaluator fields, gold actions, and gold-document lists cannot enter model payloads or planner-accessible files.
- Actual runtime involvement is demonstrated through trace lineage, not by substituting the stock agent.

**Dependencies:** PRs 1–4.

### PR 6 — Complete the retail domain and obtain the first capability score

Deliver the minimal read → confirm → mutation vertical slice first. Its successful official-harness fixture/pilot unlocks banking work; completing the remaining retail inventory and the 114-task coverage run does not block the first banking run.

**Proposed files**

- src/safe_agent/domains/retail/contracts.py
- src/safe_agent/domains/retail/projection.py
- src/safe_agent/domains/retail/policy.py
- src/safe_agent/domains/retail/preview.py
- src/safe_agent/domains/retail/responses.py

**Changes**

- Implement the full pinned retail operation inventory: identity lookup, user/order/product/item reads, calculation, address/payment/item changes, cancellation, return, exchange, and handoff. [S13]
- Encode ownership, allowed status, same-product variant selection, availability, payment method constraints, sufficient balances, cancellation reasons, and one-time modify/exchange behavior.
- Collect the complete intended item set before a one-time operation.
- Enforce authentication, one customer per conversation, explicit confirmation for updates, and one tool call at a time with no simultaneous customer message. [S13]
- Preserve exact upstream argument and error semantics.
- Use deterministic arithmetic where needed without substituting a different business environment.

**Acceptance**

- Authored fixtures cover read-only help, clarification, valid/denied mutation, multiple requests, and handoff.
- Negative cases prove stale consent, wrong owner, unsupported transitions, and repeated one-time mutations are blocked.
- Five development tasks run through the actual model and official evaluator.
- Then run all 114 retail tasks once for a coverage report. Every task is accounted for; task failures remain visible.

**Dependencies:** PR 5.

### PR 7 — Add banking retrieval and discoverable capabilities

**Proposed files**

- src/safe_agent/domains/banking_knowledge/retrieval.py
- src/safe_agent/domains/banking_knowledge/evidence.py
- src/safe_agent/domains/banking_knowledge/discovery.py
- src/safe_agent/domains/banking_knowledge/contracts.py

**Changes**

- Use the upstream bm25 retrieval profile and its KB_search tool first.
- Store observed document IDs, hashes, relevant spans, and provenance with bounded context.
- Support search → discover a documented operation → unlock → consume its parameter schema → propose the typed call.
- Validate the inner tool name and parsed arguments of call_discoverable_agent_tool, whose arguments are transmitted as a JSON string.
- Treat unlocking and user-tool grants as recorded state changes.
- The internal policy registry may know approved schema definitions, but the planner only sees discoverable capabilities after legitimate discovery and unlocking.
- Support context compaction that retains unresolved actions, authenticated subject, consent, and evidence bindings without inventing facts.

**Acceptance**

- Undiscovered, locked, fabricated, incorrectly typed, or out-of-scope inner operations cannot dispatch.
- No gold retrieval, required-document shortcut, or complete discoverable catalog is exposed to the planner.
- KB content cannot issue authority or modify runtime policy.
- Long retrieved content is bounded, with deterministic retention of the pending action and current evidence.
- Official retrieval results and timing are preserved in τ trajectories.
- An early banking smoke run demonstrates legitimate BM25 retrieval, a documented capability discovery, and a grounded response before the full banking operation catalog is required.

**Dependencies:** PRs 2–5 and a successful minimal retail integration fixture/pilot. The remainder of PR 6 can proceed in parallel.

### PR 8 — Complete banking identity, user-guided actions, and operation families

**Proposed files**

- src/safe_agent/domains/banking_knowledge/identity.py
- src/safe_agent/domains/banking_knowledge/policy.py
- src/safe_agent/domains/banking_knowledge/user_guidance.py
- src/safe_agent/domains/banking_knowledge/responses.py
- Typed operation modules grouped by banking workflow

**Changes**

- Implement the pinned identity protocol: compare customer-provided values against permitted lookup results, require two supported fields, and record verification through log_verification. Full name or user ID alone is insufficient. [S14]
- Keep customer claims separate from values read from bank records. Reading a value does not prove the customer supplied it.
- Use get_current_time for the simulated clock; never substitute the host date.
- Cover substantive operations for rewards, disputes, card replacement/closure/payment, credit limits, account opening/closure/transfers, fee corrections, debit-card lifecycle/security, and supporting reads.
- Support the user-only path: discover → give_discoverable_user_tool → explain exact usage → wait → observe/reconcile through normal interaction.
- Apply policy-specific consent and escalation rules, including legitimate KB precedence/overrides represented in the frozen policy bundle. Retrieval alone cannot change that bundle.
- Use exact decimal calculations internally with upstream-compatible argument serialization.

**Acceptance**

- User-only actions are never performed by the agent.
- Granting or explaining a user tool does not mark the requested outcome as completed.
- Identity cannot be established from values copied from a lookup instead of the customer.
- Every substantive banking operation family has an authored success case and relevant denial/boundary cases.
- Five development banking tasks exercise retrieval, identity, discovery, a mutation, user guidance, and handoff across the set.
- Then run all 97 tasks once and produce a complete coverage/failure inventory.

**Dependencies:** PR 7.

### PR 9 — Close recovery, isolation, and accounting gaps

**Proposed files**

- src/safe_agent/benchmarks/tau3/recovery.py
- src/safe_agent/benchmarks/tau3/accounting.py
- tests/benchmarks/tau3/ and focused episode/domain security tests

**Changes**

- Exercise failure before authorization, after authorization, after dispatch, after a tool result, and after journal persistence.
- Deduplicate incoming messages and result consumption using exact identifiers and hashes.
- Keep an uncertain write unresolved until supported observations establish the result.
- Do not promise general mid-episode crash resume unless both SAFE state and the upstream environment/user state can be restored consistently.
- For the initial release, an unrecoverable interrupted trial ends explicitly; a new trial starts with fresh state and a new attempt ID under a fixed retry rule.
- Account for failed calls, repairs, user simulation, required judging, optional retrieval services, and infrastructure.
- Keep evaluator artifacts and benchmark secrets outside planner access.

**Acceptance**

- No automatic write resend occurs after an ambiguous outcome.
- Completed effects are not duplicated by repeated input delivery.
- Restart cannot replenish budgets or revive expired consent.
- A process-isolation probe cannot read benchmark answer files or access execution credentials.
- Every interrupted or retried trial remains in the audit and cost record.
- Existing affected calendar/TAL/recovery regressions remain green.

**Dependencies:** PRs 5–8. Add the checks incrementally; this PR closes remaining gaps.

### PR 10 — Run the frozen comparison and prepare the research report

**Proposed files**

- src/safe_agent/benchmarks/tau3/reporting.py
- configs/benchmarks/tau3-final.toml
- docs/TAU3_EVALUATION_PROTOCOL.md
- Generated run manifests, official trajectories, and report artifacts outside source control

**Changes**

- Freeze implementation, prompts, policy bundles, retrieval, model IDs/settings, simulator, judge, and run limits.
- Run the actual stock LLMAgent baseline and SAFE with matched settings.
- Use the unchanged evaluator with EvaluationType.ALL and each task's actual reward_basis. Do not substitute DB-only grading. [S15]
- Run the complete 97-task banking domain at four trials per system.
- Generate official pass metrics and separate SAFE diagnostics.
- Prepare and validate a custom submission package with architecture, development exposure, and configuration disclosures. Public submission is a separate publication step.

**Acceptance**

- The manifest's full task × trial matrix is accounted for.
- Required trajectories and grading outputs are present; no filtered final run or selective retry improves the reported score.
- Metrics regenerate from saved records using the pinned evaluator and recorded judge outputs. Fresh LLM rejudging is a new stochastic evaluation and must be identified as such.
- The report presents successes, failures, uncertainty, costs, and limitations whether SAFE improves or loses.
- Both systems use the same retrieval configuration for the direct comparison.

**Dependencies:** PR 9.

## 5. Test and acceptance matrix

Focus tests on authority boundaries, protocol correctness, and meaningful failure modes. Do not create large suites that merely restate schema implementations.

| Scenario | Required result |
| --- | --- |
| Wrong customer/account/order reference | Denied before tool dispatch |
| Policy changes after planning | Old authorization invalid |
| Customer edits the item list after confirmation | New confirmation required |
| Tool output contains “ignore policy” instructions | No change in authority |
| Nested banking operation attempts an unapproved write | Inner operation denied |
| Agent attempts a user-only banking operation | Denied; use user-guidance flow |
| Model claims an action succeeded before a result | Critical success statement blocked |
| Timeout after an action is emitted | Outcome uncertain; no blind resend |
| Duplicate tool result | Idempotent consumption, no additional effect |
| Restart during an episode | Preserve bindings/budgets or explicitly terminate the trial |
| Budget exhausted by repairs | Stop under the configured policy |
| Private canary in a protected field | Excluded from declared model/log channels |
| Hidden evaluator sentinel | Absent from planner input and accessible filesystem |
| Legitimate action blocked by SAFE | Recorded as a false refusal for analysis |
| Old calendar workspace reopened | Existing versioned behavior preserved |

Retain the current closed-contract, broker, coordinated-planning, mandatory-TAL, recovery, subprocess, and affected PostgreSQL/SQLite migration gates. This plan does not claim those tests have been run during preparation.

## 6. Evaluation protocol and budget

### 6.1 Experiment design

The first comparison has two systems:

- Stock τ LLMAgent with model M.
- SAFE-AGENT with the same model M.

Use identical model revisions and comparable reasoning settings, official tasks, permitted tools, simulator, evaluator, retrieval configuration, and trial schedule. Match seeds where supported, recognizing that different assistant behavior produces different subsequent customer messages.

SAFE may use more model calls internally; record all of them. Report success at observed cost and, where useful, under a shared budget ceiling. Do not weaken the baseline to compensate for SAFE's overhead.

The stock baseline is a reference agent implementation. A claim about a particular competitor product requires integrating and running that product separately under comparable conditions.

### 6.2 Development exposure

Use authored synthetic fixtures for most implementation work and a declared development subset for wiring checks. Keep these decisions separate from final scoring.

Retail's base set contains its 74 train and 40 test tasks. Banking currently has no implemented held-out split. A complete base run after tuning on some of its tasks must not be called an untouched holdout. Disclose the development selection and freeze before final evaluation. [S12]

Do not encode task IDs, expected refunds, gold actions, hidden required-document lists, or evaluator outcomes into runtime rules. Generic policies derived from public domain materials are allowed custom-system work and should be disclosed.

### 6.3 Run sizes

| Stage | Workload |
| --- | ---: |
| Retail wiring pilot | 5 tasks × 1 trial × 2 systems = 10 conversations |
| Banking wiring pilot | 5 tasks × 1 trial × 2 systems = 10 conversations |
| Full retail coverage milestone | 114 tasks × 1 trial for SAFE = 114 conversations |
| Full banking coverage milestone | 97 tasks × 1 trial for SAFE = 97 conversations |
| Final banking comparison | 97 tasks × 4 trials × 2 systems = 776 conversations |
| Optional final retail comparison | 114 tasks × 4 trials × 2 systems = 912 conversations |
| Both domains, four trials, two systems | 1,688 conversations |

These are conversation counts, not API-call counts. Development runs, retries, and a second retrieval configuration add further work. Derive the final matrix from the pinned manifest rather than embedding these counts in agent logic.

### 6.4 Costs and caps

Track separately:

1. Agent operating cost: planner calls, repairs, checkers, and agent-side retrieval/runtime costs.
2. Evaluation cost: simulated customer calls, required judge calls, indexing, reviews, and experiment infrastructure.
3. Failed and interrupted attempts, including uncertain billed requests.

Calculate agent cost per successful task as total measured agent cost across the declared attempt set divided by successful task attempts. If there are no successes, report it as undefined rather than zero.

The bm25 configuration removes dense-embedding and shell requirements. Agent, user, and applicable judge inference still cost money unless supplied through suitable existing local compute or free access. [S16]

Use five-task pilots to estimate average and high-tail costs, then set run and episode caps before scaling. Keep automatic optional LLM review off initially. Preserve holds for unknown provider billing outcomes; do not treat a timeout as a free request.

The current retail tasks can require NL-assertion grading, and banking can also use non-DB reward components. Record the actual grading configuration from the pinned files rather than relying on generic documentation prose. The pinned default NL judge is gpt-4.1-2025-04-14. [S15] [S18]

### 6.5 Report

Include:

- Coverage: expected, attempted, terminal, graded, and infrastructure-failed trials.
- Official pass¹ and pass⁴.
- Paired task-level differences with uncertainty intervals.
- Agent failures versus runtime/infrastructure failures.
- Latency and tool/model calls per task.
- Agent operating cost and total experiment bill.
- Unauthorized-action attempts and actual dispatches.
- Consent violations, wrong-subject accesses, and declared sensitive-field exposure.
- Duplicate effects, unresolved outcomes, and false refusals.
- Failure categories: missing evidence, retrieval, planning, policy implementation, overblocking, tool arguments, response correctness, and simulator/evaluator issues.

With exactly four trials per task, pass⁴ measures the fraction of tasks succeeding in all four trials. Use the upstream metric implementation. Keep SAFE diagnostics separate from official reward.

Record the official evaluator's treatment of infrastructure errors. Also show all scheduled attempts and a clearly labelled conservative operational view where unresolved attempts count as failures; never silently improve denominators.

### 6.6 Submission classification

SAFE changes the agent scaffold and prompts, so prepare a custom submission. The v1.0.1 guide permits a single-domain submission, requires full coverage of each claimed domain, prefers four or more trials, and requires the banking retrieval configuration. [S17]

BM25 and AllTools results must remain separate. A later AllTools comparison requires both systems to use the same dense/sparse/shell tools, index configuration, and corpus. Do not compare a BM25 SAFE score with an unmatched published AllTools score as if it isolated architecture.

## 7. Proposed command interface

These are **interfaces to implement**, not commands currently available in SAFE-AGENT.

~~~bash
# Offline pin, dependency, data, schema and run-size inspection
python -m safe_agent.benchmarks.tau3 inspect --config configs/benchmarks/tau3-v1.0.1.toml

# Small controlled development run after the adapter and model route exist
python -m safe_agent.benchmarks.tau3 run --config configs/benchmarks/tau3-dev.toml

# Complete frozen comparison, with explicit budget limits in the config
python -m safe_agent.benchmarks.tau3 run --config configs/benchmarks/tau3-final.toml

# Build reports from saved manifests and grading outputs
python -m safe_agent.benchmarks.tau3 report --run-dir artifacts/tau3/final
~~~

The runner must register the SAFE factory before calling the upstream runner in the same process. It must distinguish scripted fixtures, stock baseline, and SAFE runs in metadata.

Required manifest fields:

- SAFE and τ commit; TAL pin; dependency lock hash.
- Domain, task content/IDs hash, declared development exposure, trial count.
- Tool schemas, policy bundle, prompt/schema revisions, and KB hashes.
- Agent and user model identifiers, reasoning/temperature parameters, and seeds.
- Grader identifier/arguments and EvaluationType.ALL.
- Retrieval profile and any embedding/reranker settings.
- Step, inference, tool, error, timeout, retry, and budget limits.
- Privacy/consent profile, output artifacts, timestamps, usage and cost method.

## 8. Risks and decisions that should remain visible

| Risk | Design response |
| --- | --- |
| A calendar-shaped runtime truncates a conversation | New episode layer with independent action completion |
| A wrapper measures the base model rather than SAFE | Require SAFE authorization lineage for every outward call |
| Broad framework work delays the first result | Start with serial turns, one provider, BM25, and the two selected domains |
| Customer confirmation is confused with operator approval | Separate benchmark execution authority, customer consent, and office approval |
| Nested discovery bypasses restrictions | Validate and classify the actual inner tool operation |
| Retrieval results acquire authority | Keep source data separate from trusted policy and capability grants |
| Sensitive data leaks through conversation or logs | Field-level projection and rendering across all channels |
| Masking destroys task-relevant information | Preserve permitted facts or use deterministic helpers over protected values |
| A timeout duplicates a business effect | Explicit uncertainty and no blind resend |
| A restarted trial is presented as resumed state | Require coherent environment recovery or use a new recorded attempt |
| Policy checks are oversold as semantic certainty | Report encoded-rule coverage separately from reasoning accuracy |
| Upstream changes invalidate comparison | Pin v1.0.1 and make 2.0 a separate experiment |

## 9. Immediate implementation handoff

Begin with **PR 0 and PR 1**.

The first reviewable deliverable should include the frozen benchmark manifest, an inventory of supported operations, episode/action schemas, an additive storage migration, the deterministic episode reducer, and an offline fixture proving two sequential actions with a customer turn between them.

Then implement PRs 2–5 as the first end-to-end integration. The first model-backed demonstration should show an authenticated retail read, an explicit action confirmation, one authorized mutation through the official orchestrator, and a response based on its observed result.

Do not begin the full paid benchmark run until the five-task pilot produces valid trajectories, the runtime/isolation gates pass, and the measured pilot cost has been converted into explicit run limits.

## Sources

[S1]: https://github.com/sierra-research/tau2-bench/blob/989d3285ae669e49503324bdb9981bf14fccedeb/README.md
[S2]: https://github.com/sierra-research/tau2-bench/releases/tag/v1.0.1
[S3]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/ci/tal-office-commit.sha
[S4]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/contracts/inference.py
[S5]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/inference/gateway.py
[S6]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/storage/coordinated_models.py
[S7]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/evidence/ledger.py
[S8]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/broker/references.py
[S9]: https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/tal/trusted_dispatch.py
[S10]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/agent/README.md
[S11]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/pyproject.toml
[S12]: https://github.com/sierra-research/tau2-bench/tree/fc0055dc4e0a316c3f83133267fbd6faaa770992/data/tau2/domains
[S13]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/data/tau2/domains/retail/policy.md
[S14]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/data/tau2/domains/banking_knowledge/prompts/components/additional_instructions.md
[S15]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/evaluator/evaluator.py
[S16]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/knowledge/README.md
[S17]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/docs/leaderboard-submission.md
[S18]: https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/config.py

Additional implementation references:

- [Inference service](https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/src/safe_agent/inference_service/service.py)
- [W4 coordinated planning](https://github.com/AWDaley/SAFE-AGENT/blob/b9ab733088d54c92a6608f90c90418592d0b3ddc/docs/W4_COORDINATED_PLANNING.md)
- [Retail tool implementation](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/domains/retail/tools.py)
- [Retail split manifest](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/data/tau2/domains/retail/split_tasks.json)
- [Banking tool implementation](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/domains/banking_knowledge/tools.py)
- [Banking environment and task loader](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/domains/banking_knowledge/environment.py)
- [Banking policy header](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/data/tau2/domains/banking_knowledge/prompts/components/policy_header.md)
- [Runner construction and agent boundary](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/runner/build.py)
- [Pinned model and judge defaults](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/src/tau2/config.py)

