"""Pinned admission schemas; no dispatcher or live model authorization."""

from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.contracts.inference_admission import (
    InferenceAdmissionPolicy,
    InferenceAdmissionReceipt,
    InferenceThroughputLimits,
)

TARGET = Path(__file__).resolve().parents[1] / "schemas/safe-agent-m7-admission-v1.json"


def export() -> bytes:
    return canonical_json({
        "contract_set": "safe_agent.m7.inference_admission.v1",
        "schemas": {
            "policy": InferenceAdmissionPolicy.model_json_schema(),
            "receipt": InferenceAdmissionReceipt.model_json_schema(),
            "throughput": InferenceThroughputLimits.model_json_schema(),
        },
    }) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != export():
            raise SystemExit("M7 admission schemas differ; regenerate and review")
    else:
        TARGET.write_bytes(export())


if __name__ == "__main__":
    main()
