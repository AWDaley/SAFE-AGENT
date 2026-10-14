"""Export/check strict, versioned M0 JSON contracts (no external dependencies)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from safe_agent.agent import SafeTaskContext
from safe_agent.contracts import (
    ActionDecision,
    Approval,
    CalendarCreateEvent,
    EvidenceEvent,
    ExecutionReceipt,
    Projection,
    RequestClarification,
    ScopedReference,
    Simulation,
    Task,
    TaskBudget,
    TaskManifest,
)
from safe_agent.contracts.actions import Proposal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "schemas/safe-agent-m0-contracts-v1.json"
    classes = (
        Task, TaskBudget, TaskManifest, ScopedReference, Projection, Simulation,
        CalendarCreateEvent, RequestClarification, ActionDecision, Approval,
        ExecutionReceipt, EvidenceEvent, SafeTaskContext,
    )
    payload = {
        "contract_set": "safe_agent.m0.contracts.v1",
        "note": "Registry of standalone strict JSON schema documents; authorization remains TAL-side.",
        "contracts": {item.__name__: item.model_json_schema() for item in classes},
        "Proposal": TypeAdapter(Proposal).json_schema(),
    }
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != data:
            raise SystemExit("schema export out of date: run python scripts/export_schemas.py")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
