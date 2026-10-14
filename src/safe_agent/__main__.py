"""Offline synthetic planning demonstration; no provider writes or TAL authorization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.contracts import Projection, Task
from safe_agent.orchestration import TaskRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE-AGENT M0 (planner only)")
    parser.add_argument("command", choices=["demo"])
    options = parser.parse_args()
    if options.command == "demo":
        fixture = Path(__file__).resolve().parents[2] / "fixtures/synthetic_office/demo.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        result = TaskRunner(FakePlanner()).plan_once(
            Task.model_validate_json(json.dumps(data["task"])),
            SafeTaskContext.model_validate_json(json.dumps(data["context"])),
            Projection.model_validate_json(json.dumps(data["projection"])),
        )
        print(result.model_dump_json(indent=2))
        print("M0 ONLY: proposal not authorized, not executed, and not verified.")


if __name__ == "__main__":
    main()
