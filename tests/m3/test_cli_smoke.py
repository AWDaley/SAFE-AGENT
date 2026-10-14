"""End-to-end command reports independently verified synthetic evidence."""
from __future__ import annotations

from safe_agent.demo.m3 import run_m3_demo


def test_m3_demo_has_before_after_and_hash_linked_completion():
    payload = run_m3_demo()
    assert payload["environment"] == "SYNTHETIC_FIXTURE_ONLY"
    assert payload["provider_network_access"] is False
    assert payload["upstream_tal_calendar_domain_certified"] is False
    assert payload["effect_count"] == 1
    assert payload["after"]["booking_count"] == payload["before"]["booking_count"] + 1
    assert payload["before"]["source_hash"] != payload["after"]["source_hash"]
    assert payload["receipt"]["outcome"] == "COMMITTED"
    assert payload["snapshot"]["phase"] == "COMPLETED"
    assert payload["snapshot"]["verification_hash"].startswith("sha256:")
    assert [item["kind"] for item in payload["timeline"]][-2:] == [
        "ACTION_INTENT_RECORDED", "ACTION_VERIFIED",
    ]
    result = str(payload)
    for forbidden in ("cust_c1", "cal_main_private", "@example.com", "access_token"):
        assert forbidden not in result
