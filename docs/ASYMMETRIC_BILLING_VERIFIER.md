# Asymmetric billing verifier boundary

Status: offline/synthetic verification only. This increment does not enable
hosted inference, real provider billing, live business providers, or production
authorization.

## Goal

SAFE-AGENT must not possess the same secret used to mint the billing evidence it
accepts. The legacy HMAC fixture is useful for regression tests, but a process
holding that HMAC key can both create and verify an attestation.

The new verification path uses Ed25519:

- the isolated billing-verifier process holds the private signing key;
- the reconciliation process receives only the 32-byte public key;
- the source-signing key is also confined to the verifier boundary in this
  synthetic topology;
- the verifier has no SAFE-AGENT database, broker, operator token, inference
  dispatch key, or business-provider interface.

Possession of the public key cannot create a valid attestation.

## Process layout

```text
synthetic billing source record
        |
        | source HMAC
        v
isolated billing verifier
  - source verification key
  - Ed25519 private key
  - verifier-only workload token
        |
        | VerifiedBillingAttestation
        | Ed25519 signature
        v
trusted reconciliation process
  - Ed25519 public key only
  - inference cost ledger
        |
        v
RECONCILED accounting record
```

The verifier listens only on an owner-only Unix-domain socket. The client performs
no TCP, DNS, redirect, proxy, or automatic retry fallback.

## Attestation

`VerifiedBillingAttestation` binds:

- request ID;
- organization and task;
- route and model revision;
- original prompt digest;
- charge in integer micro-USD;
- source-evidence digest;
- issuer identity;
- verifier key ID;
- verification timestamp;
- fixed `ed25519` algorithm;
- 64-byte Ed25519 signature encoded as 128 lowercase hex characters.

The service parses a bounded closed `SyntheticBillingSourceRecord`, rejects
duplicate JSON keys and non-finite constants, verifies the source HMAC and age,
then signs the canonical attestation payload.

## Reconciliation

`InferenceBudgetLedger.reconcile_verified` requires:

- exactly 32 public-key bytes;
- the configured issuer ID;
- the configured verifier key ID;
- a valid Ed25519 signature;
- the existing exact request/task/route/model/prompt binding;
- charge no larger than the original reservation;
- no prior attestation for that receipt.

Successful reconciliation changes accounting only. It does not approve a task,
recover a model response, queue a provider action, certify a business effect, or
authorize replanning.

The existing PR #40 rules still apply: only a reconciled `UNKNOWN` invocation can
stop consuming an outstanding admission slot. A financially reconciled
`DISPATCH_STARTED` invocation remains outstanding until normal invocation
recovery marks it `UNKNOWN`.

## Executable verifier

The installed command is:

```sh
safe-agent-billing-verifier \
  --socket /private/run/billing.sock \
  --host billing.example.com \
  --workload-token-file /private/secrets/billing-workload.token \
  --source-key-file /private/secrets/billing-source.key \
  --issuer-private-key-file /private/secrets/billing-ed25519.key \
  --issuer-id independent_billing_verifier_v1 \
  --verifier-key-id billing_key_2026_01
```

The socket directory must be owner-only. Secret files must be owner-only regular
files with no hard links or symlink traversal. The command does not accept a
database URL.

## Current limitation

The accepted source record is still synthetic and HMAC-signed. This validates
process/key separation and reconciliation mechanics, not a real vendor invoice.
A future provider adapter must obtain evidence from a billing source independent
of the model/inference service and map that provider evidence into the closed
verification input without exposing provider credentials to the agent.

The legacy HMAC `BillingChargeAttestation` reconciliation method remains only for
existing compatibility tests. New integration work should use the asymmetric path.
