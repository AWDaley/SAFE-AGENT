"""Pin additive M3 synthetic-execution and internal-task wire contracts."""
from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.evidence.ledger import LedgerEvent, TaskSnapshot

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "schemas/safe-agent-m3-contracts-v1.json"


def export() -> bytes:
    payload = {
        "contract_set": "safe_agent.m3.contracts.v1",
        "note": "Synthetic effects only; model does not authorize provider writes.",
        "schemas": {
            name: cls.model_json_schema()
            for name, cls in sorted({
                "internal_task_record": InternalTaskRecord,
                "internal_task_manifest": InternalTaskManifest,
                "internal_task_proposal": InternalTaskCreateProposal,
                "task_snapshot_v2_compatible": TaskSnapshot,
                "office_event_v2_compatible": LedgerEvent,
            }.items())
        },
    }
    return canonical_json(payload) + b"\n"


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--check", action="store_true")
    args = cli.parse_args()
    expected = export()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != expected:
            raise SystemExit("M3 contract schemas differ; regenerate and review")
        print("M3 schema export matches pinned bytes")
    else:
        TARGET.write_bytes(expected)
        print(TARGET)


if __name__ == "__main__":
    main()
