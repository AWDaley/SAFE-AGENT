"""Pinned offline coordinator wire and receipt schemas."""
from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.contracts.inference_coordinator import (
    CoordinatedInferenceEnvelope,
    InferenceDispatchReceipt,
)

TARGET = Path(__file__).resolve().parents[1] / "schemas/safe-agent-m7-coordinator-v1.json"


def export() -> bytes:
    return canonical_json({
        "contract_set": "safe_agent.m7.inference_coordinator.v1",
        "schemas": {"envelope": CoordinatedInferenceEnvelope.model_json_schema(),
                    "receipt": InferenceDispatchReceipt.model_json_schema()},
    }) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != export():
            raise SystemExit("M7 coordinator schemas differ; regenerate and review")
    else:
        TARGET.write_bytes(export())


if __name__ == "__main__":
    main()
