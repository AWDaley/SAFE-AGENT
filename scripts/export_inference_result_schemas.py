"""Print W3 JSON schemas or check their pinned canonical digests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from safe_agent.canonical import sha256
from safe_agent.contracts.inference_results import InferenceResultRecovery, ValidatedInferenceResult

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "schemas" / "inference_result_schema_hashes.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--hashes", action="store_true")
    args = parser.parse_args()
    schemas = {model.__name__: model.model_json_schema()
               for model in (ValidatedInferenceResult, InferenceResultRecovery)}
    digests = {name: sha256(value) for name, value in schemas.items()}
    if args.check:
        if json.loads(PIN.read_text()) != digests:
            print(json.dumps(digests, indent=2, sort_keys=True))
            raise SystemExit("W3 schema hashes differ from reviewed pins")
        print("W3_RESULT_SCHEMA_HASHES=PASS")
    else:
        print(json.dumps(digests if args.hashes else schemas, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
