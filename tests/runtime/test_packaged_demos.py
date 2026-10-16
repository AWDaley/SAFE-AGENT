"""Bundled demo inputs must match the existing, synthetic-only test fixtures."""
from pathlib import Path

import pytest

from safe_agent.demo import synthetic_fixture_directory
from safe_agent.virtual_office.state import SyntheticOfficeState

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["demo.json", "office_state.json"])
def test_packaged_demo_bytes_match_original_fixture(name):
    assert (synthetic_fixture_directory() / name).read_bytes() == (
        ROOT / "fixtures/synthetic_office" / name
    ).read_bytes()


def test_demo_input_location_is_independent_of_current_directory(tmp_path, monkeypatch):
    expected = synthetic_fixture_directory()
    monkeypatch.chdir(tmp_path)
    assert synthetic_fixture_directory() == expected
    source = SyntheticOfficeState.from_fixture(expected / "office_state.json")
    assert source.organization_id == "org_demo"


def test_missing_explicit_input_does_not_fall_back_to_demo_data(tmp_path):
    with pytest.raises(FileNotFoundError):
        SyntheticOfficeState.from_fixture(tmp_path / "missing_customer_input.json")
