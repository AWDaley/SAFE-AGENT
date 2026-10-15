# M7-01 — Provider-neutral inference contracts and offline mock

**Status:** Trusted-bootstrap integration only. Uses the merged M6 task planner,
Docker isolation and synthetic workflow foundation. **M5 live Google
acceptance remains BLOCKED.** No networked model integration is enabled.

## Purpose

Define one closed, versioned inference-only interface so SAFE-AGENT can later
use a vetted model provider without giving models access to office credentials,
raw provider identifiers, task grants, SQL, network browsing, or execution tools.

M7-01 does NOT implement remote provider transport, API key management,
network egress gateway or a verified usage/billing ledger. Its only runnable
backend is a deterministic offline mock.

## Contract boundaries

The following typed interfaces live in src/safe_agent/contracts/inference.py:

- InferenceRoutePolicy: trusted route and model revision, fixed
  proposal/clarification capability, request/response byte ceilings,
  conservative token proxies, timeout and estimated micro-USD cost.
  No URL, API token, credential, tool schema or arbitrary operation list.
- InferenceRequest: freshly generated request ID, route/model ID, prompt
  hash, issued/deadline times, minimized SafeTaskContext and Projection.
  Cross-task/tenant/calendar mismatches, stale source or incorrect hashes
  are rejected.
- InferenceTransportReply: UNTRUSTED backend output, exact echo-binding
  fields and optional independently UNVERIFIED reported token usage.
- InferenceOutcome: validated typed proposal or sanitized clarification,
  hashes and explicit false execution/authorization flags.

The provider-neutral InferenceTransport protocol is an infer(request)
method returning InferenceTransportReply. Only trusted bootstrap code
may select a backend. No model/browser API may configure endpoints or
change provider, model, permissions, timeout, token or spending limits.

## Admission sequence

1. Existing ClosedJsonPlanner builds a canonical envelope containing
   the fixed instruction, scoped task context and synthetic availability.
2. InferenceGateway rejects noncanonical/duplicate JSON, unknown fields,
   raw private properties, wrong scope, stale observations or inconsistent
   pending-simulation handles.
3. The gateway checks strict UTF-8 size, conservative token proxy and
   worst-case estimated per-call price BEFORE invoking the backend.
   It mints a fresh request ID, route/model binding and deadline.
4. The trusted backend receives only the typed minimized request; it
   has no resolver, SQL repository, office credential or tool registry.
5. The gateway verifies the exact reply ID/route/model/prompt binding,
   deadline, response bytes and the unverified reported-usage ceiling.
6. It re-parses the answer with the existing closed-schema parser and
   checks calendar/customer reference, template, time zone, revision,
   pending simulation, allowed window, exact duration and free slot.
7. Model-written clarification questions are replaced by a fixed safe
   sentence. Backend exceptions and raw responses are never copied
   to the operator UI.
8. Output remains data: Virtual Office, TAL-style policy, separate human
   approval, durable synthetic outbox and independently verified readback
   retain all execution authority.

Echo-binding prevents protocol mix-ups but does NOT authenticate a
malicious provider. A network adapter needs an independently authenticated
channel, egress policy and verified usage records.

## Offline wiring

    from safe_agent.agent.json_planner import ClosedJsonPlanner
    from safe_agent.contracts.inference import InferenceRoutePolicy
    from safe_agent.inference import InferenceGateway, OfflineMockInferenceTransport
    from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning

    route = InferenceRoutePolicy(
        route_id="offline_mock",
        model_revision="mock_calendar_v1",
        max_estimated_cost_microusd=0,
    )
    gateway = InferenceGateway(route, OfflineMockInferenceTransport())
    planner = TrustedPersistentPlanning(
        planning_store, synthetic_workflow,
        planner=ClosedJsonPlanner(gateway.generate),
    )

The mock uses the standard-library-only deterministic offline protocol
fixture. It does NOT call a hosted language model or contact the network.
No new public HTTP route is added. The existing operator planning,
bounded attempts and approval flow are unchanged.

## Acceptance coverage and CI

Run pytest -q tests/m7, the complete test suite, Ruff, mypy, the
offline Docker smoke and pinned TAL interop. Tests must demonstrate:

- Closed schema with no credential, URL or execution grant fields.
- Canonical prompt minimization and tenant/task/calendar binding.
- Bounded input, price preflight and late reply rejection.
- Tampered transport ID/revision/digest rejection.
- Arbitrary tool requests, model-declared completion, duplicate JSON,
  malformed responses and output flooding cannot pass admission.
- Permission/window/template/slot widening fails closed.
- Clarification question and backend exception text remain private.
- Token usage is labeled UNVERIFIED; reported counts beyond budget deny.
- Existing M6 persistent planning can call the mock gateway with zero
  office effects. There is no direct /v1/inference endpoint.

## Next PR — M7-02 restricted inference-only gateway

The future hosted adapter must run with isolated inference credentials,
reviewed model-route configuration, authenticated workload connections,
allowlisted HTTPS egress, redirect/DNS checks, bounded request/response
and service-level enforcement of timeouts. External model access should
remain outside the no-network Docker task container.

Follow with durable usage reservation and provider reconciliation.
Reported token counts are not verified charges. A timeout with uncertain
usage must not be counted as free. Do not enable production credentials,
live Google writes or unrestricted agent tools in M7-01.
