"""Explicitly distinguish offline CI preparation from live M5 acceptance."""
from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.release_gate import inspect_m5_release


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-executable SAFE-AGENT M5 gate")
    parser.add_argument("--mode", choices=("offline", "live"), required=True)
    args = parser.parse_args(argv)
    report = inspect_m5_release(root or Path(__file__).resolve().parents[1])
    print(report.model_dump_json(indent=2))
    if report.offline_configuration != "CONFIGURED":
        return 1
    # Critical: no attestations, flags, or local JSON can assert that a
    # Google-hosted event was written and independently verified.
    return 0 if args.mode == "offline" else 2


if __name__ == "__main__":
    raise SystemExit(main())
