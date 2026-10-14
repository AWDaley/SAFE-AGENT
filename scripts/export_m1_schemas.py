"""Export versioned, sanitized M1 interface contracts.

Private fixtures and reference binding records are intentionally excluded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from safe_agent.virtual_office.models import (
    CalendarDiff,
    CalendarProjection,
    CalendarSimulation,
    InternalTaskChange,
    InternalTaskSimulation,
    ObserveCalendar,
    ObserveTasks,
    SafeTaskSummary,
    TaskDiff,
    TaskListProjection,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema_file = Path(__file__).resolve().parents[1] / "schemas/safe-agent-m1-contracts-v1.json"
    classes = (
        ObserveCalendar, CalendarProjection, CalendarDiff, CalendarSimulation,
        ObserveTasks, SafeTaskSummary, TaskListProjection,
        InternalTaskChange, TaskDiff, InternalTaskSimulation,
    )
    payload = {
        "contract_set": "safe_agent.m1.contracts.v1",
        "note": "Advisory, side-effect-free Virtual Office schemas; no private data or authority.",
        "contracts": {item.__name__: item.model_json_schema() for item in classes},
    }
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not schema_file.exists() or schema_file.read_text(encoding="utf-8") != data:
            raise SystemExit("M1 schema export out of date: run scripts/export_m1_schemas.py")
    else:
        schema_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file.write_text(data, encoding="utf-8")
        print(schema_file)


if __name__ == "__main__":
    main()
