"""Reproducible closed invocation contracts; no runtime dispatch capability."""

from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationEvent,
    InferenceInvocationView,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "schemas/safe-agent-m7-dispatch-v1.json"


def export() -> bytes:
    return canonical_json({
        "contract_set": "safe_agent.m7.inference_dispatch.v1",
        "note": "Schema/replay foundation only. No runtime admission, billing proof or effects.",
        "schemas": {
            "invocation_binding": InferenceInvocationBinding.model_json_schema(),
            "invocation_event": InferenceInvocationEvent.model_json_schema(),
            "invocation_view": InferenceInvocationView.model_json_schema(),
        },
    }) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = export()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != expected:
            raise SystemExit("M7 invocation schemas differ; regenerate and review")
    else:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_bytes(expected)


if __name__ == "__main__":
    main()
