"""Explicit synthetic demonstrations and their packaged, non-customer fixtures."""
from pathlib import Path


def synthetic_fixture_directory() -> Path:
    """Locate bundled demo inputs independently of the checkout or working dir.

    This is only used by explicit demonstrations. A missing customer input or
    failed production configuration must never fall back to these fixtures.
    """
    return Path(__file__).resolve().parent / "fixtures"
