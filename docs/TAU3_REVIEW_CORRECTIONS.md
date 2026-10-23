# Benchmark privacy and completion review corrections

These corrections address two reproduced runtime-boundary defects in the
authored offline benchmark profile. They do not supply model-backed task scores
or production approval.

## Payment-secret projection

The explicit field policy now treats `pin`, `current_pin`, `old_pin`, `new_pin`,
and `cvv` as payment secrets. PIN aliases share the `pin` reference kind; CVV
uses a separate kind. Structured values are projected before sibling free text,
and the native banking table presentation follows the same field policy.

Customer and tool text also recognizes explicit PIN/CVV labels followed by
bounded numeric codes. A bare numeric customer reply following an explicit
PIN/CVV prompt in host-recorded assistant history is protected. If the prompt
asked for both kinds, the reply becomes an unclassified payment-secret
reference that cannot resolve into a tool argument; a subsequent explicit
customer label is needed. Ordinary unrelated numbers are not treated as
credentials merely because they have three or four digits.

Secret references retain the existing episode, actor, subject, operation,
resource-kind and expiry checks. The trusted tool path resolves them into the
native arguments immediately before normal domain admission. The generic
customer-scalar fallback does not accept literal secret arguments, so a model
cannot substitute guessed digits even when those digits occur in customer
history. Free-form response rendering rejects secret-reference interpolation,
known literal secret echoes, and numeric PIN/CVV assertions. Rejection feedback
uses a fixed code and does not copy the rejected secret into the next prompt.

The scope remains the declared synthetic benchmark formats. This is not a
general detector for every possible spelling, encoding, arbitrary prose, or
secret category. Production intake needs its own reviewed policy and dedicated
secret-entry handling. Internal trusted context and exact outgoing arguments
still contain the native values; opaque model references do not imply encrypted
storage or protection from a privileged host.

## Completion evidence

A discoverable banking read can cause the official wrapper to record a call
log. Its dispatch effect therefore remains `write` for conservative recovery.
That logging side effect previously caused the read to become a `business_write`
completion receipt.

Domain decisions and pending calls now carry a separate `business_effect` from
the underlying operation. The result receipt uses that effect, while retaining
`dispatch_effect` for the wrapper's retry classification. Successful reads and
controls cannot satisfy the mutation-completion renderer; successful writes can.
This fixes the effect-type confusion without weakening no-resend protection or
claiming complete semantic verification of the requested business outcome.

## Persistence and regression evidence

`synthetic_benchmark_field_policy_v2` and
`tau3-business-effect-receipts-v2` are bound into both run and episode policy
hashes. A prior run cannot silently reuse old projection or receipt semantics;
use a new declared run after changing profiles rather than relabelling old
evidence.

`tests/episodes/test_episode_review_boundaries.py` exercises the actual runtime,
gateway and scripted transport. It checks model-request payloads, typed secret
resolution, literal/reference echo rejection, numerical data preservation,
ambiguous intake, and the wrapped-read completion defect. A successful PIN-change
fixture verifies that native tool arguments remain usable while both preceding
and subsequent model requests keep the synthetic secrets opaque. Existing
domain and actual pinned-harness tests retain independent coverage.
