"""Deterministic schema export for M2 trusted office-domain contracts."""

from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.evidence.ledger import LedgerEvent, TaskSnapshot
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import Principal
from safe_agent.tal.office_gate import CompiledOfficeGrant

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "schemas/safe-agent-m2-contracts-v1.json"


def export() -> bytes:
    payload = {
        "schema_version": "safe_agent.m2_schemas.v1",
        "schemas": {
            name: cls.model_json_schema()
            for name, cls in sorted({
                "approval_input": ApprovalInput,
                "compiled_office_grant": CompiledOfficeGrant,
                "ledger_event": LedgerEvent,
                "operator_principal": Principal,
                "task_snapshot": TaskSnapshot,
            }.items())
        },
        "notice": "Provisional office domain, not upstream TAL v0.3 conformance",
    }
    return canonical_json(payload) + b"\n"


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--check", action="store_true")
    args = cli.parse_args()
    expected = export()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != expected:
            raise SystemExit("M2 contracts schema export differs; regenerate and review")
        print("M2 schema export matches pinned bytes")
    else:
        TARGET.write_bytes(expected)
        print(TARGET)


if __name__ == "__main__":
    main()
