"""Resolve the single reviewed TAL CI commit SHA, refusing shell injection."""
from __future__ import annotations

from pathlib import Path

from safe_agent.release_gate import read_tal_pin


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        sha = read_tal_pin(root)
    except (ValueError, OSError):
        # Never print untrusted pin content or install a default branch.
        print("TAL_PIN_INVALID_OR_MISSING", file=__import__("sys").stderr)
        return 2
    print(sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
