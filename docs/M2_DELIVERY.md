# M2 delivery — Offline TAL-style authority, durable office ledger and approvals

**Status:** Implementation complete in SAFE-AGENT's synthetic environment. **NOT** upstream TAL calendar conformance, production authorization, or a live calendar integration.

## Implemented components

| Scope | Implemented | Verification |
| --- | --- | --- |
| M2-01 | Immutable manifest registration, task pinning, deterministic office grant compilation (provisional) | Manifest hash and task/tenant binding tests |
| M2-02 | Closed one-object proposal parser, state-version and scope checks, time/operation/budget limits, broker re-resolution | Invalid-target/operation/version tests, zero effect records |
| M2-03 | Tenant-scoped approval workflow, separate approving actor, hash/manifest/expiry binding, readiness revalidation | Approval, replay, expiry and changed-hash tests |
| M2-04 | Versioned Alembic PostgreSQL migration and SQLAlchemy tables for tasks, manifests, protected references, projections, simulations, proposals, approvals, reserved execution records and evidence | SQLite migration tests; ephemeral PostgreSQL migration and trigger smoke in CI |
| M2-05 | Hash-linked append-only ledger, separately increasing audit sequence/state version, pure replay, PostgreSQL immutability triggers | Tamper/replay tests and restarted SQLite file |
| M2-06 | M0/M1 `PlanningResult` to trusted `TrustedOfficeTasks` bridge, offline eligibility gate, no dispatcher | End-to-end synthetic demo and CI; zero provider effects |
| M2-07 | Signed and expiring local bearer tokens, tenant/role checks at operator API and broker, sanitized audit outputs | API attack/identity tests |

## Local reproduction

```bash
python -m pip install -e '.[dev,api]'
pytest
python scripts/export_schemas.py --check
python scripts/export_m1_schemas.py --check
python scripts/export_m2_schemas.py --check
python -m safe_agent m2-demo
```

The demo prints `M2_SYNTHETIC_POLICY_ONLY_NO_EXTERNAL_EXECUTION` and `external_effects: 0`. A policy `AUTHORIZED` decision in its output does **not** authorize provider writes. The M2 service has no provider dispatcher and cannot report a business task `COMPLETED`.

## PostgreSQL migration (trusted deployment preparation only)

Use a dedicated PostgreSQL database and a restricted application role. Do not use the superuser role in production.

```bash
python -m pip install -e '.[dev,api,postgres]'
export SAFE_AGENT_DATABASE_URL='postgresql+psycopg://<app_role>:<password>@<host>/<database>'
alembic upgrade head
```

Do **not** use actual credentials in ChatGPT or committed configuration files. `postgresql+psycopg` is required by `LedgerRepository.production_engine`. `LedgerRepository.testing_sqlite()` alone uses `Base.metadata.create_all()`; production schema changes must go through Alembic. M2 PostgreSQL smoke tests execute on an isolated, disposable GitHub Actions service database and verify that the audit triggers reject writes.

## Hosted API for local evaluation only

The factory `safe_agent.api.create_app(coordinator, authenticator, clock=...)` intentionally needs a trusted coordinator and `SignedOperatorTokens` instance. There is no externally callable token-issuance endpoint and no unauthenticated way to register/manipulate a task grant. `GET /health`, `GET /v1/tasks/{id}`, `/events`, `/replay`, and `POST /v1/tasks/{id}/approvals` are available. The local API does **not** expose a model tool, a provider adapter, arbitrary database access, or customer records.

## Remaining before M3

- Implement an actual TAL calendar-domain action manifest/compiler, translation layer and upstream-compatibility tests against pinned TAL revision `23d48c4`; do not use the provisional positive office decision to enable real tool calls.
- Define an idempotent fake-calendar dispatcher behind the *actual* TAL gate. Re-check live source state/expiry and the approval hash at dispatch, then independently verify a committed effect.
- Add durable outbox, uncertain-outcome reconciliation, provider-version conditional operations, and startup recovery (M4).
- Integrate production-grade SSO/OIDC, key rotation, row-level tenant access, database secret management and independent evidence anchoring before business deployment.

## Truthful completion wording

M2 establishes **policy eligibility and durable evidence in a synthetic office**. It does **not** create a calendar appointment, grant external-provider permissions, finish the scheduling task, certify security compliance, or prove upstream TAL calendar compatibility.
