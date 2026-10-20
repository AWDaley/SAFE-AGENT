"""python -m safe_agent.benchmarks.tau3 {inspect,run,report,diagnostics}."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .diagnostics import DiagnosticsError, report_diagnostics
from .inventory import inspect_benchmark
from .reporting import report_saved_run
from .runner import run_official


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned τ³ text integration for SAFE-AGENT")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--tau-root", type=Path)
    report = commands.add_parser("report")
    report.add_argument("--run-dir", required=True, type=Path)
    diagnostics = commands.add_parser("diagnostics", help="Read-only aggregate runtime diagnostics")
    source = diagnostics.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--database", type=Path)
    diagnostics.add_argument("--run-id")
    diagnostics.add_argument("--annotations", type=Path)
    diagnostics.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "diagnostics":
        database = args.database or args.run_dir / "private/episodes.sqlite"
        output = args.output or (args.run_dir / "diagnostics.json" if args.run_dir else None)
        try:
            result = report_diagnostics(
                database, output=output, run_id=args.run_id, annotations=args.annotations
            )
        except DiagnosticsError as exc:
            print(
                json.dumps(
                    {
                        "kind": "aggregate_runtime_diagnostics",
                        "status": "unavailable",
                        "reason": str(exc),
                    }
                )
            )
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["integrity"]["passed"] else 2
    elif args.command == "report":
        result = report_saved_run(args.run_dir)
    else:
        config = load_config(args.config)
        if args.command == "inspect":
            result = inspect_benchmark(config, args.tau_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ready" else 2
        output = run_official(config, tau_root=args.tau_root)
        result = report_saved_run(output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
