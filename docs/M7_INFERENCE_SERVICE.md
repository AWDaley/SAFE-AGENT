# M7-04 — Isolated offline inference service and synthetic billing verifier

**Status:** Trusted-host *offline* service acceptance. Stacked on PR #27,
which depends on PR #26 and PR #25. Independent security review and
protected main-branch rules are required before merging or deployment.
**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.**

## Budget migration conflict resolution — PR #27 is authoritative

PR #27 is the selected M7-03 budget foundation. PR #29 introduced an
independently developed, incompatible `0009_inference_budget` migration
and a distinct reservation/reconciliation API. PR #29 has been closed as
superseded; its branch was preserved, not merged.

PR #28 MUST continue to target PR #27 and retain the single migration
`0009_inference_cost_accounts` with parent `0008_planning_workspace`.
The M7-04 synthetic verifier imports PR #27's
`BillingChargeAttestation`, and calls
`InferenceBudgetLedger.reconcile_attested` from PR #27.
It must NOT import PR #29's budget contracts or migration.

The regression script `scripts/check_m7_budget_lineage.py` validates
the one-head Alembic ancestry, rejects any competing `0009` migration,
checks the canonical account/reservation/event/attestation ORM columns,
and checks the verifier-to-ledger method signatures. Under the SQLite
CI fixture it also verifies the **actual migrated database** revision,
tables and columns; the separate M7 integration tests verify a synthetic
billing attestation and budget settlement against that selected interface.

Changes to M7 budget semantics require a separate **forward Alembic
migration**, not a replacement of an already selected `0009` revision.
Do not merge PR #29 or cherry-pick its historical conflicting migration.
PRs #25, #26, #27 and #28 remain awaiting independent review; choosing
a migration does not authorize production deployment or Google writes.

## 1. What runs where

M7-04 adds a minimal, independently hosted ASGI inference-only service.
It is **not** the SAFE-AGENT operator API and has no route for task
creation, approval, Google events, outbox claims, database writes,
credential resolution, billing settlement or generic model tool calls.

The only supported backend is the **exact deterministic
OfflineMockInferenceTransport**. The service cannot be configured to
call a hosted LLM or open inference egress; this is intentional until the
remaining network isolation and verified billing controls exist.

The trusted operator gateway continues to own task authorization,
the immutable manifest, M7-03 cost reservations and the TAL gate.
It can call the M7-02 restricted HTTPS transport, which should reach an
independent TLS/mTLS proxy. The proxy authenticates the service identity
and forwards exactly /v1/infer into the sidecar's permission-restricted
Unix-domain socket. The model sidecar owns only the inbound workload token
and the schema-limited *synthetic* inference handler. It must NEVER have
database, broker, Google OAuth, provider-writing or Docker-daemon access.

The bundled service does NOT implement that external TLS/mTLS proxy or a
real provider adapter; it is exercised with in-process adversarial tests
and a separate real UDS process/HTTP smoke test.

## 2. Trusted service controls

TrustedInferenceServicePolicy is immutable. It pins:

- Exact route and model revision and expected HTTP Host.
- Upper limits on UTF-8 request/response bytes.
- Maximum concurrently admitted requests (default two).
- Maximum requests in a bounded in-memory rate window.
- One-use request ID replay tracking with bounded memory (default 2048)
  and a TTL longer than the maximum 120-second request deadline.

TrustedWorkloadAuthenticator uses a constant-time SHA-256 digest
comparison of a high-entropy inference-only Bearer token supplied by the
trusted host. Duplicate/missing Authorization or mismatched Host headers
are refused *before request-body reading*. Tokens are never placed in
request models, application logs or response payloads. Authentication
does not replace the proxy's TLS/mTLS workload-identity validation.

The handler accepts only a single closed versioned InferenceRequest JSON
object and refuses duplicate keys, nonfinite JSON, invalid MIME, nonidentity
content encoding, wrong route/model scope, expired deadlines, or a body
larger than the configured byte cap. A request ID is marked used *before*
the backend executes. Even failed/late mock inference cannot reuse it
within the process lifetime.

The backend's typed reply must match request ID, route, model, and prompt
hash; the existing closed proposal parser and immutable task/calendar
scope validator remain mandatory. Output never authorizes an effect.
No operator can inspect a request's customer handle or model output
from a health or operational endpoint.

### Endpoints

- GET /healthz: no credentials, fixed ready/mode indication only.
- GET /internal/status: workload-authenticated aggregate counters only;
  no tenant/task IDs, customer data, model text or credentials.
- POST /v1/infer: workload-authenticated one-request/one-reply protocol
  returning only InferenceTransportReply. 401 unauthorized, 400 malformed,
  413 too large, 415 invalid media, 409 replay, 429 capacity/rate,
  503 worker failure. Error bodies are empty and Cache-Control is no-store.

Swagger and OpenAPI endpoints are disabled. No public billing or execution
endpoint is created.

### Operational limitations

The process-local rate limiter and replay cache reset after service
restart. The existing M7-03 SQL ledger must still enforce durable request
IDs and conservative budget holds in the trusted caller. A Python
thread-pool job that hangs cannot be forcibly killed by an ASGI task
cancellation; only the exact synthetic mock is admitted. Any future
untrusted or paid provider transport requires a separately sandboxed
process with a hard OS deadline and verified cancellation/failure recovery.

The /healthz response is deliberately privacy-minimal, not a durable
alerting system. Deployment needs monitored process and socket state,
restart/incident policy, credential rotation, bounded ingress timeouts,
external metrics retention with privacy constraints, egress firewalling
and task/tenant throughput budgets.

## 3. Running a local separate-process demonstration

Install the pinned dependencies from requirements.lock, including Uvicorn.
Use an owner-only directory and an owner-only token file:

    mkdir -m 700 -p /tmp/safe-infer-demo
    printf '%s' 'synthetic_ONLY_replace_with_unique_32_plus_byte_secret' \
      > /tmp/safe-infer-demo/workload.token
    chmod 600 /tmp/safe-infer-demo/workload.token

    export SAFE_INFER_SOCKET_DIR=/tmp/safe-infer-demo
    export SAFE_INFER_WORKLOAD_TOKEN_FILE=/tmp/safe-infer-demo/workload.token
    export SAFE_INFER_HOST=inference.example.com
    export SAFE_INFER_ROUTE=approved_offline_route
    export SAFE_INFER_MODEL_REVISION=offline_mock_v1
    python -m safe_agent.inference_service

The CLI **does not bind a TCP port**. It creates a Unix socket named
inference.sock under SAFE_INFER_SOCKET_DIR and refuses any preexisting
socket/path, symlink or unsafe directory/token-file permissions. Uvicorn
is configured without access logs or forwarded-proxy header trust.

The CI-run integration script is:

    python scripts/smoke_inference_sidecar.py

It starts a separate Python service process, connects through UDS via an
httpx client, confirms unauthorized requests fail, validates one signed
workload token and bounded typed synthetic response, rejects a duplicate
request, reads sanitized counters and terminates the service. It never
reaches the public network or any live model account.

A deployment-hardening *example* is provided at
deploy/inference-sidecar.systemd.example. It is not a proven or audited
production unit; the operator must verify identity, owner-only service
credentials, UDS and proxy permissions, rootless operation, OS network
isolation and systemd sandbox functionality in its deployment.

## 4. Independent synthetic billing source verifier

M7-04 also introduces OfflineSyntheticBillingVerifier, a separate
source-verification module. It accepts only a closed
SyntheticBillingSourceRecord whose source_kind is
synthetic_offline_only. It authenticates the statement using a dedicated
source HMAC key, checks the timestamp, amount, request/task/route/model
binding, and deterministically generates a single HMAC-attested
BillingChargeAttestation using a DIFFERENT trusted issuer key.

The existing M7-03 ledger can then authenticate that attestation and
reconcile the held amount. The test covers a partial charge, unused
reservation release, replay/forgery refusal and no business effects.
The inference service does NOT import or expose the billing verifier,
source key, invoice evidence, ledger or reconciliation route.

**Evidence qualification:** Authenticating an independent *synthetic*
source fixture does NOT verify an external vendor invoice, prove token
consumption or establish production billing truth. HMAC alone also
cannot verify vendor records. A future production integration must
verify genuine provider-issued usage/invoice records through the
authorized provider API or a cryptographically verifiable source,
reconcile over-ceiling charges and store appropriate audit evidence.

## 5. Review and release gates

Run:

    pytest -q tests/m7/test_inference_sidecar.py
    pytest -q tests/m7/test_offline_billing_verifier.py
    python scripts/smoke_inference_sidecar.py
    python scripts/check_m7_budget_lineage.py
    pytest -q tests/m7/test_budget_lineage.py
    pytest
    mypy src/safe_agent
    ruff check src tests scripts
    python scripts/check_m5_release.py --mode offline

CI still checks M5/M6 synthetic postconditions, the real Docker
no-network demo, SQL migration head, and pinned TAL conformance.

Required before production are an audited, hardened inference-only host;
actual mTLS ingress proxy configuration; authenticated and isolated
service key provisioning/rotation; OS-level egress prevention; bounded
hard deadlines; external vendor adapter validation; independently
verified provider billing source; durable service replay/rate state or
equivalent central admission; monitoring/incident alerting; restored
PR approval/branch protection; and real hosted end-to-end acceptance.

Never run this service with Google credentials, unrestricted model tools,
an exposed Docker daemon, or direct public unauthenticated TCP access.
**M5 LIVE GOOGLE ACCEPTANCE REMAINS BLOCKED.**
