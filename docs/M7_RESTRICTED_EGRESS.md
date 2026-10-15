# M7-02 — Restricted inference-only HTTPS egress

**Status:** Opt-in trusted-host transport, initially verified with synthetic
DNS/TLS responders. Stacked on PR #25 (M7-01), which is still awaiting
independent review. **M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.**

## Scope and trust separation

The only model-visible layer remains the closed schema of M7-01.
This increment provides a restricted provider-neutral HTTPS *transport* for
an independently hosted inference gateway. It can transmit a strictly
versioned InferenceRequest to a trusted **inference-only HTTPS sidecar**
that implements the M7 reply schema. It does not implement a vendor-specific
OpenAI/Anthropic API mapping, create an OAuth connection, create a task
execution endpoint, or grant the model authority over software.

The operator UI cannot select a host, path, bearer token, model, source
credential, IP, TLS mode, or HTTP command. The configuration and credential
resolver exist in trusted bootstrap, outside task and model inputs.

A correct deployment must separate the sidecar process/host and its
inference-only key material from the SAFE-AGENT operator web process,
business provider worker, reference broker and privileged SQL ledger.
The Python module alone cannot enforce OS process or container isolation.

## Trusted configuration

TrustedInferenceEgressPolicy contains only an exact FQDN, normalized
single path, route ID, model revision, request/response byte ceilings
and maximum network duration. IPv4/IPv6 literals, localhost-style names,
metadata domains, userinfo, URL schemes, alternate ports, percent
escapes, path traversal, queries and fragments are denied at construction.

InferenceCredentialSource is a trusted-host protocol which returns a
pydantic SecretStr for an inference-only authentication token. The module
never reads environment variables, an office credential store, Google
OAuth keys or model-generated token strings. The bearer secret is
retrieved **after** request validation and DNS checks, used only in
the HTTPS Authorization header and never serialized to the task model,
database, URL, log message or exception text.

## Pinned-TLS network restrictions

1. Reparse the entire typed InferenceRequest, including the canonical
   prompt digest, tenant/actor/task/calendar scope and pending simulation.
   Reject route/model mismatch, already expired deadlines and oversized
   serialized messages BEFORE reading credentials.
2. Resolve only the configured exact trusted hostname, HTTPS port 443.
   Every DNS answer must be a globally routable IP; mixed private and
   public answers are denied. Localhost, RFC1918, link-local, cloud
   metadata, unique-local IPv6, multicast and reserved addresses fail
   closed.
3. Open TCP to the **checked numeric IP**, never re-resolving the
   hostname during the connection. Use the original trusted FQDN for
   certificate hostname verification and TLS SNI. Rely on the OS trust
   store and default certificate validation. Do not accept an insecure
   TLS override or arbitrary proxy environment variables.
4. Issue exactly one HTTPS POST to the fixed normalized path, with JSON
   and an inference-only bearer token. No redirects, arbitrary tools,
   methods, query parameters, client URLs or HTTP connection reuse.
5. Require HTTP 200, JSON content type and identity encoding. Bound
   the response using its Content-Length and the decoded byte stream,
   reject redirect/429/500 and never copy response/SSL error details
   into any task or audit message.
6. Validate a single strict InferenceTransportReply whose request ID,
   model revision, route and prompt hash match the request. The M7-01
   InferenceGateway then independently checks exact proposal types,
   customer/calendar references, task window, duration and allowed slot.
7. Model output remains untrusted. It can only be considered by the
   existing Virtual Office simulation, TAL-style gate, human approval,
   durable synthetic outbox and independent verifier.

The egress module does not log prompt contents, tokens, provider results,
DNS answers or task/customer handles.

## Sidecar protocol

The HTTPS sidecar accepts the JSON serialization of the existing
safe_agent.inference_request.v1 model. It returns a closed
safe_agent.inference_transport_reply.v1 object containing only:

- request_id / route_id / model_revision / prompt_digest
- output_json: exactly one SAFE-AGENT proposal/clarification JSON string
- optional reported_usage: unverified backend token counts

No sidecar should execute business software, load Google credentials,
resolve private broker handles or expose SQL. The separate sidecar
may in a future PR map the closed request to an approved hosted model,
but only with a similarly reviewed provider-specific adapter.

The echo fields are protocol consistency checks, not proof of backend
identity. Production requires an authenticated and authorized sidecar
service identity, audited inference keys and a network segmentation policy.

## Trusted bootstrap (illustrative)

    from pydantic import SecretStr
    from safe_agent.inference.restricted_https import (
        RestrictedHTTPSInferenceTransport, TrustedInferenceEgressPolicy,
    )
    from safe_agent.inference import InferenceGateway
    from safe_agent.contracts.inference import InferenceRoutePolicy

    class TrustedSidecarKeySource:
        def load_inference_token(self) -> SecretStr:
            # Implement only in the isolated gateway host, not in the
            # operator process or synthetic model container.
            return trusted_inference_secret_source.load()

    egress = TrustedInferenceEgressPolicy(
        route_id="approved_inference_route",
        model_revision="pinned_model_revision",
        host="approved-inference.example.com",
        path="/v1/infer",
    )
    transport = RestrictedHTTPSInferenceTransport(
        egress, TrustedSidecarKeySource(),
    )
    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id=egress.route_id,
            model_revision=egress.model_revision,
        ),
        transport,
    )

The service and provider route above are EXAMPLES, not a published live
endpoint or an implementation of a particular model vendor. M7-03
introduces an opt-in InferenceBudgetLedger, which MUST be installed
at trusted gateway bootstrap for any future paid inference. Reserved
cost stays held until independent charge evidence is reconciled, even
after a successful HTTP inference response. See M7_INFERENCE_BUDGETS.md.
Do not
deploy the module inside the web process, agent Docker container or
provider dispatch process. Never expose it to operator JSON or a model tool.

## M7-04 isolated offline inference-only service

M7-04 adds an independently launchable ASGI sidecar listening only on
an owner-restricted Unix socket. An external, independently reviewed
TLS/mTLS proxy would forward the fixed inference route to the socket.
The sidecar verifies an inference workload token, bounds rate and
concurrency, and refuses replays or arbitrary provider tools; it
runs the exact offline mock only. No live vendor inference or Google
operations are connected. See M7_INFERENCE_SERVICE.md.

## Testing and deployment blockers

tests/m7/test_restricted_https.py uses a synthetic sidecar wire responder
and synthetic DNS resolver. It verifies strict hostname/path policies,
public-only DNS, refusal of mixed/private answers before credential
retrieval, exact numeric connection targets, TLS hostname SNI, closed
request metadata, invalid/out-of-scope requests, sanitized failures,
non-redirect handling, response bounds and fake integration with M7-01.
The CI suite also retains the **actual** no-network M6 Docker model smoke.

These tests do not contact a real model provider, present production
certificates or perform external office effects. They prove protocol and
network-confinement implementation, not production isolation.

Release blockers still requiring independent work:

- A dedicated inference sidecar host, separately authenticated workload
  identity and inference-only secret custody/rotation.
- External network policy/firewall restricting all egress to approved IPs;
  the Python allowlist is defense in depth, not an OS network sandbox.
- DNS lookups in Python can block before the socket deadline. A production
  sidecar must enforce process-level deadlines or use a bounded trusted
  DNS service with monitored resolver timeouts. Slow-trickle TLS responses
  need process-level watchdogs for hard end-to-end guarantees.
- Verified model-vendor endpoint/adapters, authenticated real HTTP/TLS
  integration tests, provider-usage verification, quotas and billing
  reconciliation before real hosted inference.
- Tenant-level budget reservation and uncertain-charge recovery (M7-03),
  held-out adversarial scenarios, durable operator alerts and independent
  security review of transport/authorization.
- GitHub branch protection remains outstanding; PR #25 must be reviewed
  and merged before this stacked feature.
- Hosted Google acceptance remains BLOCKED; no production customer
  calendars or OAuth grants are permitted through this inference path.

**Never claim provider token reports or local cost estimates as verified
billing evidence. Never treat inference output as execution permission.**
