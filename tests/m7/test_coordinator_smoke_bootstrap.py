"""The process acceptance script must not rely on ambient PYTHONPATH/cwd."""
import os
import subprocess
import sys
from pathlib import Path


def test_smoke_fixture_bootstrap_in_isolated_interpreter(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "smoke_inference_coordinator.py"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
         "assigned,policy=namespace['_load_fixtures'](); "
         "assert assigned.__module__ == 'tests.m7.admission_fixtures'; "
         "assert policy().route.model_revision == 'offline_v1'",
         str(script.resolve())],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
