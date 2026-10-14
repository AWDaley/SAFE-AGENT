# ADR 0006 — TAL execution authority stored with SAFE outbox

**Status:** PR 1, offline integration only; real Google execution disabled.

Upstream TAL PR 5 provides tal.office.execution_authorization.v1. TAL's original
OfficeAdmissionDecision continues to return authorizes_execution=False.
SAFE-AGENT provides a protected, unique SQL row bound to a calendar outbox action.
A trusted coordinator can persist this row inside LedgerRepository.write_task()
alongside its existing execution intent and evidence ledger transaction. The
write method verifies tenant, actor, task, target handle, pinned manifest and
proposal, state version, budgets, approval digest, action hash and expiry.

The read method checks canonical integrity and the persisted outbox correlation.
The future real provider worker must ALSO call TAL's fresh require method with
the reconstructed action before any first effect. Reading a record alone is
never sufficient permission, and the current synthetic worker is unchanged.
This PR adds no external tool or Google write route.

TAL's SQLite journal is an audit mirror, not an atomic execution permit.
SHA-256 catches uncoordinated tampering but is not a keyed signature: trusted
database access and immutability controls must protect these records.
This record is not model-visible. Further provider binding, replay defense,
runtime authorization and process isolation remain explicit PR 2/3 work.
