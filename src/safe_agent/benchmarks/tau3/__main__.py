"""Frozen τ³ execution, saved-result analysis, and private submission preparation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .config import load_config
from .diagnostics import DiagnosticsError, report_diagnostics
from .inventory import inspect_benchmark
from .reporting import report_saved_run
from .runner import run_official


def _write_new_report(path: Path, value: dict[str, Any], inputs: tuple[Path, ...]) -> None:
    target = path.absolute()
    if any(target.resolve().is_relative_to(source.resolve()) for source in inputs):
        raise ValueError("COMPARISON_OUTPUT_MUST_BE_OUTSIDE_INPUT_RUNS")
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned τ³ text integration for SAFE-AGENT")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "run", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--tau-root", type=Path)
        if name == "preflight":
            command.add_argument("--offline", action="store_true",
                                 help="Check configuration without reading keys or contacting a socket")
            command.add_argument("--output", type=Path)
        elif name == "run":
            command.add_argument("--qualification-report", type=Path,
                                 help="Passed report required for every canonical local SAFE run")
            command.add_argument("--qualification-review", type=Path,
                                 help="Attributed semantic review required for canonical local SAFE runs")
    qualification = commands.add_parser("qualify", help="Run bounded authored local model checks")
    qualification.add_argument("--config", required=True, type=Path)
    qualification.add_argument("--tau-root", type=Path)
    qualification.add_argument("--output-dir", required=True, type=Path)
    qualification.add_argument("--max-seconds", type=float, default=3600.0)
    calibration = commands.add_parser("calibrate", help="Read-only reservation analysis of a saved pilot")
    calibration.add_argument("--run-dir", required=True, type=Path)
    calibration.add_argument("--target-config", required=True, type=Path)
    calibration.add_argument("--headroom-factor", type=float, default=1.5)
    calibration.add_argument("--output", type=Path)
    report = commands.add_parser("report")
    report.add_argument("--run-dir", required=True, type=Path)
    diagnostics = commands.add_parser("diagnostics", help="Read-only aggregate runtime diagnostics")
    source = diagnostics.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--database", type=Path)
    diagnostics.add_argument("--run-id")
    diagnostics.add_argument("--annotations", type=Path)
    diagnostics.add_argument("--output", type=Path)
    comparison = commands.add_parser("compare", help="Compare matched saved SAFE/stock runs")
    comparison.add_argument("--safe-run-dir", required=True, type=Path)
    comparison.add_argument("--baseline-run-dir", required=True, type=Path)
    comparison.add_argument("--bootstrap-samples", type=int, default=2000)
    comparison.add_argument("--seed", type=int, default=300)
    comparison.add_argument("--failure-annotations", type=Path)
    comparison.add_argument("--output", type=Path)
    submission = commands.add_parser("prepare-submission", help="Validate a private custom bundle")
    submission.add_argument("--run-dir", required=True, type=Path)
    submission.add_argument("--metadata", required=True, type=Path)
    submission.add_argument("--output", required=True, type=Path)
    submission.add_argument("--tau-root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "calibrate":
        from .calibration import CalibrationError, calibrate_pilot

        try:
            target = load_config(args.target_config)
            result = calibrate_pilot(args.run_dir, target,
                                     headroom_factor=args.headroom_factor)
            if args.output:
                _write_new_report(args.output, result, (args.run_dir, Path(target.output_dir)))
        except (CalibrationError, ValueError, OSError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, CalibrationError) else "INVALID_CALIBRATION_INPUT_OR_OUTPUT"
            print(json.dumps({"kind": "pilot_reservation_calibration", "status": "unavailable",
                              "reason": reason}))
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "review_required" else 2
    elif args.command == "compare":
        from .comparison import ComparisonError, compare_saved_runs

        try:
            result = compare_saved_runs(
                args.safe_run_dir, args.baseline_run_dir,
                bootstrap_samples=args.bootstrap_samples, seed=args.seed,
                failure_annotations=args.failure_annotations,
            )
            if args.output:
                _write_new_report(args.output, result, (args.safe_run_dir, args.baseline_run_dir))
        except (ComparisonError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ComparisonError) else "INVALID_COMPARISON_INPUT_OR_OUTPUT"
            print(json.dumps({"kind": "paired_comparison", "status": "unavailable", "reason": reason}))
            return 2
    elif args.command == "prepare-submission":
        from .submission import SubmissionError, prepare_submission

        try:
            result = prepare_submission(args.run_dir, args.metadata, args.output,
                                        tau_root=args.tau_root)
        except (SubmissionError, ValueError, OSError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, SubmissionError) else "INVALID_SUBMISSION_INPUT_OR_OUTPUT"
            print(json.dumps({"kind": "private_custom_submission_bundle", "status": "unavailable",
                              "reason": reason}))
            return 2
    elif args.command == "diagnostics":
        database = args.database or args.run_dir / "private/episodes.sqlite"
        output = args.output or (args.run_dir / "diagnostics.json" if args.run_dir else None)
        try:
            result = report_diagnostics(
                database, output=output, run_id=args.run_id, annotations=args.annotations,
                run_dir=args.run_dir,
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
        try:
            config = load_config(args.config)
        except (ValueError, OSError):
            print(json.dumps({"status": "unavailable", "reason": "INVALID_BENCHMARK_CONFIG"}))
            return 2
        if args.command == "inspect":
            result = inspect_benchmark(config, args.tau_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ready" else 2
        from .preflight import PreflightError, preflight_run

        if args.command == "qualify":
            from .qualification import QualificationError, run_qualification

            try:
                result = run_qualification(
                    config, args.output_dir, tau_root=args.tau_root,
                    max_seconds=args.max_seconds,
                    progress=lambda event: print(json.dumps(event, sort_keys=True), flush=True),
                )
            except PreflightError as exc:
                print(json.dumps(exc.report, indent=2, sort_keys=True))
                return 2
            except (QualificationError, OSError) as exc:
                reason = str(exc) if isinstance(exc, QualificationError) else "QUALIFICATION_IO_FAILED"
                print(json.dumps({"kind": "authored_local_qualification", "status": "blocked",
                                  "reason": reason}))
                return 2
            print(json.dumps({key: value for key, value in result.items()
                              if key not in {"preflight", "probes", "schedule"}},
                             indent=2, sort_keys=True))
            return 0 if result["status"] == "automated_checks_passed" else 2
        if args.command == "preflight":
            result = preflight_run(config, tau_root=args.tau_root, offline=args.offline)
            if args.output:
                try:
                    _write_new_report(args.output, result, (Path(config.output_dir),))
                except (OSError, ValueError):
                    print(json.dumps({"kind": "benchmark_preflight", "status": "blocked",
                                      "reason": "INVALID_PREFLIGHT_OUTPUT"}))
                    return 2
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] in {"ready_for_attempt", "configuration_validated"} else 2
        from .qualification import QualificationError

        try:
            output = run_official(config, tau_root=args.tau_root,
                                  qualification_report=args.qualification_report,
                                  qualification_review=args.qualification_review)
        except QualificationError as exc:
            print(json.dumps({"status": "blocked", "reason": str(exc)}))
            return 2
        except PreflightError as exc:
            print(json.dumps(exc.report, indent=2, sort_keys=True))
            return 2
        result = report_saved_run(output)
        # Preserve legacy launcher semantics. Opt-in controlled development runs
        # distinguish stopped/ungraded experiments from complete graded execution.
        if config.stop_after_infrastructure_errors is not None:
            manifest = json.loads((output / "manifest.json").read_text())
            coverage = result.get("coverage", {})
            if (manifest["status"] != "complete" or coverage.get("ungraded", 0)
                    or coverage.get("infrastructure_failures", 0)):
                print(json.dumps(result, indent=2, sort_keys=True))
                return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
