# First improvement increment: baseline and offline boundaries

Base revision: `f6af6f5e517d0c13accdae98e3d1befcd405f396` (M7-05c).
PR: #35. PR #34 remains a separate draft for later persistent-result integration.
This is an implementation for independent review, not security sign-off.

## Reproduced baseline

Regression-only commit `cb12310c4bd4656042f450367c95b3016e28353f`:
SAFE-AGENT checks run `38826069072`, job `116733709918`, reported
**5 failed, 576 passed, 27 skipped**. All five failures are the new expected
cases: mounted calendar/OAuth transport admitted at construction, mounted
calendar/OAuth replacement admitted before claim, and arbitrary unbudgeted
inference backend accepted by the legacy gateway. The full prior tests were
not silently marked passed if skipped. PostgreSQL and TAL interop jobs in that
run succeeded; later checks in the failing main job were skipped.

Direct cloning/DNS and Docker/PostgreSQL were unavailable in the implementation
container. Repository-wide verification therefore uses GitHub Actions. A
standalone local binding smoke used installed HTTPX 0.28.1; that is not a local
rerun of the project suite. Final fix CI must be checked on the exact PR head.

## Implemented boundary changes

The Google worker now pins each HTTP client, MockTransport and handler. It
rejects mounted routing, environment configuration, auth callbacks, request/
response hooks, redirects, subclassing, closed clients and method replacement.
Checks run before claiming, before provider operations, and after insert before
readback. Changing even to another valid offline client requires a new worker.
`create_offline_google_client` supplies a fixed-configuration fixture constructor.

Existing strictly validated base HTTPX fixture clients remain supported in this
increment; the planned factory-only/repository-owned-handler migration is NOT
claimed complete. Handlers are trusted Python. These checks are configuration
hardening, NOT confinement of malicious code or protection from root compromise.
W7 OS/process/egress acceptance remains necessary.

The legacy `InferenceGateway` now accepts only the exact offline mock, including
when a budget ledger is supplied. Unsupported mode flags and substituted backend
objects are denied. Protocol fault injection lives only in tests. Canonical
REMOTE-bound budget accounting, uncertainty and coordinator dispatch semantics
remain unchanged; no ledger or historical migration is rewritten.

`create_application_inference(mode="coordinated_offline", admission=..., transport=...)`
constructs only the existing attempt-scoped coordinator. Its API still requires
an authenticated principal, actual planning lease and canonical minimized prompt.
There is no stateless generate callback or fallback mode. This is a construction
boundary, not completion of W3/W4: M6 operator planning is NOT yet wired to it.

## Verification required before merge

Run the main workflow, mandatory PostgreSQL job, pinned TAL interop/journal jobs,
schema checks, Ruff/mypy, M6 planning/worker smoke, Docker isolation smoke, and
coordinator caller/sidecar restart smoke. Review the exact head SHA and tests
actually executed. Keep this PR draft until independent review and any remaining
CI failures are resolved. Do not merge merely because the initial regression
failure is expected.

## Outstanding work (not silently completed)

- W0: full transitive/hash lock and build-input audit; dependency vulnerability
  inventory/disposition; enforceable main-branch review rules; unified capability
  manifest. The connected GitHub app exposes no administrative write action.
- W1: migrate all supported fixtures to factory-only repository-owned handlers;
  demonstrate OS no-egress with separate identities.
- W2/W3/W4: register only the coordinated factory in the eventual supported
  application bootstrap, persist validated candidates atomically, and connect
  persistent planning without creating new attempts on uncertain retries.
- W5+: complete end-to-end application, operational reconciliation, provider
  postconditions, real-model evaluation and explicitly separate live acceptance.

**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED. Hosted models remain disabled. No customer
credentials, provider calls or production enablement are added by this PR.**
