# OpenAI organization-cost audit adapter

Status: aggregate financial audit only. This adapter does **not** reconcile one
SAFE-AGENT inference request and cannot release a per-request accounting hold.

## Why aggregate-only

As reviewed on 2026-10-16, OpenAI's organization Costs API returns daily cost
buckets and can group results by project ID and line item. It does not expose an
individual request ID in the cost result. OpenAI's Usage API can provide more
granular token/activity data, but OpenAI documents that usage may not reconcile
perfectly with Costs and recommends Costs for financial reconciliation.

Therefore SAFE-AGENT deliberately refuses to derive a billed per-request charge
from:
- response `usage` token counters;
- organization Usage API buckets;
- pricing-table multiplication;
- a project/day aggregate Costs result.

Scale Tier, discounts, billing adjustments, service tiers and other billing
semantics can make a computed usage estimate differ from financial cost.

## Credential boundary

SAFE-AGENT does not receive an OpenAI Admin API key in this increment.

A separate trusted fetcher can call:

`GET /v1/organization/costs`

with:
- UTC day-aligned start/end;
- `bucket_width=1d`;
- grouping by `project_id` and `line_item`;
- complete pagination.

The fetcher packages the response pages into
`safe_agent.openai_costs_capture.v1` and sends that capture over the existing
private billing-verifier Unix socket.

The isolated verifier has no business-provider write capability. It validates the
capture and signs only a `VerifiedProviderCostAudit`.

## Capture validation

`OpenAIOrganizationCostsAdapter` requires:

- exact endpoint identity `/v1/organization/costs`;
- USD-only amounts;
- exact day-aligned query boundaries;
- one-day buckets;
- continuous, ordered bucket coverage;
- cursor continuity between captured pages;
- a final page with `has_more=false`;
- no duplicate JSON keys or non-finite values;
- bounded project IDs and line-item labels;
- exact decimal aggregation without rounding beyond the audit contract;
- recent capture time under the verifier policy.

Incomplete pagination, a missing/duplicated day, broken cursor chain, wrong
currency, excessive decimal precision, a future capture, or a usage/response-shaped
payload fails closed.

JSON numeric amounts and decimal strings retain their exact decimal values.
Each amount must fit 16 integer digits and 12 fractional decimal places after
trailing zeroes are removed; unsupported input precision is rejected before
summation, even when several such inputs would sum to a representable amount.
Aggregation uses integer units of `10^-12` USD and is independent of the process's
Decimal precision or rounding settings. The aggregate must fit the same bound.

## Signed result

The signed audit records:

- provider = `openai`;
- adapter ID = `openai_organization_costs_v1`;
- source-evidence hash;
- observed source time;
- covered UTC window;
- exact normalized USD total;
- bucket/result counts;
- project IDs and line items;
- `request_reconciliation_supported=false`;
- block reason `PROVIDER_COSTS_ARE_AGGREGATED`.

The billing verifier signs that aggregate audit with its rotating Ed25519 verifier
key. This signature proves only that the isolated verifier accepted the captured
provider-cost evidence.

## Deliberate non-capability

`VerifiedProviderCostAudit` is a different contract from
`VerifiedBillingAttestation`.

It cannot be passed to `InferenceBudgetLedger.reconcile_verified`. Tests require
that attempting to do so leaves the original inference reservation `IN_FLIGHT`,
with the full held amount unchanged.

This means aggregate provider cost evidence can be used for financial audit and
future aggregate reconciliation tooling without creating an unsafe shortcut around
the existing per-request uncertainty rules.

## Future provider fetcher

A later increment can add a separately isolated OpenAI Admin API fetcher that owns
the Admin credential, enforces the exact query parameters above, captures all pages,
and forwards only the credential-free capture to the billing verifier.

That fetcher must remain separate from:
- the model/inference sidecar;
- the operator/browser process;
- the business execution worker;
- the request-level accounting ledger.

Until a provider supplies trustworthy one-to-one request cost evidence, OpenAI
aggregate costs remain audit-only.
