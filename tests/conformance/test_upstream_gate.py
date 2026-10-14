"""Optional pin-check, deliberately separate from the passing offline M0 suite.

Run `SAFE_AGENT_REQUIRE_UPSTREAM_TAL=1 pytest -q tests/conformance/test_upstream_gate.py`
after installing exact upstream revision to exercise this gate.
"""

from __future__ import annotations

import os

import pytest


def test_real_tal_package_contract_when_explicitly_required() -> None:
    if os.getenv("SAFE_AGENT_REQUIRE_UPSTREAM_TAL") != "1":
        pytest.skip("M0 office-domain TAL compatibility is not certified; opt in explicitly")
    from tal.adapters import Proposal, parse_proposal_json  # type: ignore[import-not-found]
    from tal.core import CANONICALIZATION_ID, TAL_VERSION  # type: ignore[import-not-found]
    from tal.runtime import TALRuntime  # type: ignore[import-not-found]

    assert TAL_VERSION == "0.3.0"
    assert CANONICALIZATION_ID == "tal-json-c14n-0.2"
    assert callable(parse_proposal_json)
    assert hasattr(TALRuntime, "apply_proposal")
    assert Proposal is not None
    # Passing this smoke probe still does not certify calendar-domain semantics.
