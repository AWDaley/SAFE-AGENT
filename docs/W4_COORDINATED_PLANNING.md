# W4 coordinated calendar planning

Initial implementation for review in PR #34, stacked on PR #36. Not merged or production-authorized. Validation evidence belongs in the PR against the exact tested commit.

The new trusted `create_coordinated_planning(store, workflow, admission=..., transport=...)` factory returns a `TrustedPersistentPlanning`-compatible service for the existing `create_app(..., persistent_planning=...)` operator endpoints. It has no injected text generator or fallback planner. It requires the shared repository, broker, virtual office and synthetic read/queue gateway.

Forward migration 0013 adds immutable task-scoped input and consumption evidence. An input captures the original minimized context, full sanitized calendar projection, original lease, policy and private-source fingerprint before inference admission. A recovered result is never paired with a fresh observation. The exact original projection can be reissued against unchanged source data using its original timestamps; current reference permissions, template duration and independent synthetic provider content are checked again.

The coordinator alone performs admission, budget holding and fenced inference dispatch. Result consumption revalidates stored input/result/history and source under the task lock, then records trusted simulation, final proposal, approval-required event, planning outcome and consumption link in one transaction. The final action differs from the candidate only by its trusted simulation reference. Inference/result hashes never authorize execution or approval.

Only approval-required synthetic calendar tasks are supported in this increment. Human approval and queueing use the existing separate endpoints; the synthetic worker independently verifies its effect. No live connector or hosted model is enabled.

An unresolved attempt returns the existing fixed HTTP 409 response and remains RUNNING in `/planning`. There is deliberately no automatic retirement, second attempt, lease renewal, refund or retry fallback, including after UNKNOWN or expiry. Clarification records are idempotently returned with fixed reason codes; creating a new planning attempt on the same coordinated input is blocked. A richer uncertainty UI and reviewed explicit retirement/replanning are follow-ups, not implemented features.

Apply the forward migration before selecting the new factory. Old fixture planning remains available for existing offline tests/demos; it is not the coordinated factory. Do not switch an active workspace to a legacy planner. Existing production isolation, restore, key-management and independent-review limitations remain. The database-owner/root compromise threat is not solved by hashes or mutation guards.
