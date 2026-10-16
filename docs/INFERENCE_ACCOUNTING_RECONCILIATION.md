# Inference accounting reconciliation and outstanding-capacity recovery

Status: offline/trusted-boundary increment. This does not certify real vendor billing,
authorize business effects, enable hosted inference, or create an operator refund action.

## Diagnosed issue

SAFE-AGENT already had two durable accounting layers:

1. monetary reservations in `safe_inference_budget_reservations`, and
2. admission/concurrency limits based on `safe_inference_invocations`.

An independent billing attestation could reconcile a monetary hold correctly, but an
invocation in `UNKNOWN` continued to count against global/tenant outstanding limits
forever. That meant independently verified accounting evidence could restore monetary
capacity while the separate admission slot remained permanently blocked.

The two ledgers therefore disagreed about whether the inference request was still
economically outstanding.

## Corrected rule

The request-rate window and the outstanding-call limit remain separate controls.

`RESERVED`
: Always outstanding. A billing record cannot turn an undispatched request into a
  completed dispatch.

`DISPATCH_STARTED`
: Always outstanding, even if financial evidence arrives. The caller must first reach
  its original deadline/recovery boundary. Billing evidence is not proof that the
  response path completed.

`UNKNOWN`
: Outstanding unless all of the following are simultaneously true:

- the canonical reservation status is `RECONCILED`;
- its monetary hold is zero;
- an immutable billing-attestation row exists for the exact request;
- the attestation digest matches the reservation evidence hash;
- the attested charge equals the reservation's charged amount; and
- the billing evidence was accepted no earlier than the recorded dispatch start.

When those conditions hold, the inference is still **outcome unknown**, but it is no
longer an unresolved accounting/concurrency item. A different authorized task may use
an outstanding slot.

`RESPONSE_VALIDATED` and `FAILED_BEFORE_DISPATCH`
: Already terminal for admission-outstanding accounting under the existing lifecycle.

## What does not change

Reconciliation does not:

- erase `UNKNOWN`;
- create or recover a model result;
- authorize replanning of the same stale task;
- approve or queue an action;
- prove that a business effect did or did not occur;
- remove a charged amount;
- reset a rate-limit window;
- accept provider/model-reported token counts as billing evidence;
- expose a refund/reset HTTP endpoint.

A zero-dollar independently attested charge is still accounting evidence, not a
shortcut around request-rate limits.

## Evidence boundary

The existing `BillingChargeAttestation` HMAC authenticates the reviewed verifier that
asserted a charge. It does not itself prove a real vendor invoice. The current
`OfflineSyntheticBillingVerifier` accepts only source-signed synthetic records and is
test evidence, not a production invoice reader.

Production paid inference still requires a genuinely independent billing source,
separate verifier identity/key custody, and provider-specific evidence review.

## Failure behavior

If status says `RECONCILED` but the immutable attestation is missing, the evidence
hash differs, the charged amount differs, or the dispatch timestamp is inconsistent,
SAFE-AGENT counts the `UNKNOWN` request as outstanding.

Thus partial/corrupt accounting state fails closed.

If financial reconciliation is recorded while the invocation is still
`DISPATCH_STARTED`, the slot remains occupied until the normal invocation recovery
moves the request to `UNKNOWN`. No financial record can skip that lifecycle gate.

## Acceptance criteria

The increment must prove that:

- unreconciled `UNKNOWN` still blocks a one-slot global/tenant policy;
- valid independent attestation settles the hold and releases only the outstanding
  slot of an `UNKNOWN` invocation;
- the real charged amount remains charged;
- `DISPATCH_STARTED` remains outstanding after financial reconciliation;
- the same financially reconciled request becomes releasable only after normal
  dispatch recovery marks it `UNKNOWN`;
- request-rate history still blocks new admission inside its window;
- inconsistent reconciliation metadata fails closed;
- real PostgreSQL repeats the critical capacity-release/live-dispatch cases; and
- all existing restart, TAL, recovery, launcher, migration and no-live-provider gates
  remain green.
