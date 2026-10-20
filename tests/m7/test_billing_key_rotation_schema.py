"""0014 billing verifier identity migration and legacy-evidence preservation."""
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7.test_inference_dispatch_schema import migrate, sqlite_engine


def test_0013_legacy_billing_attestation_survives_0014_with_explicit_legacy_identity(
    tmp_path: Path,
):
    path = tmp_path / "billing_rotation.sqlite"
    migrate(path, "0013_coordinated_planning")
    engine = sqlite_engine(path)
    repo = LedgerRepository(engine)
    try:
        args = assigned(repo)
        admission = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
        receipt = admission.admit(*args)
        request_id = receipt.binding.request_id
        legacy = {
            "receipt_id": "bil_" + "a" * 32,
            "request_id": request_id,
            "issuer_id": "legacy_hmac_fixture",
            "charge_microusd": 7,
            "source_evidence_hash": "sha256:" + "b" * 64,
            "attestation_digest": "sha256:" + "c" * 64,
            "accepted_at": NOW,
        }
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO safe_inference_billing_attestations "
                "(receipt_id, request_id, issuer_id, charge_microusd, "
                "source_evidence_hash, attestation_digest, accepted_at) "
                "VALUES (:receipt_id, :request_id, :issuer_id, :charge_microusd, "
                ":source_evidence_hash, :attestation_digest, :accepted_at)"
            ), legacy)
            before = db.execute(text(
                "SELECT receipt_id, request_id, issuer_id, charge_microusd, "
                "source_evidence_hash, attestation_digest, accepted_at "
                "FROM safe_inference_billing_attestations"
            )).mappings().one()
    finally:
        repo.close()

    migrate(path)
    actual = inspect(engine)
    columns = {item["name"] for item in actual.get_columns(
        "safe_inference_billing_attestations",
    )}
    assert {
        "verification_scheme", "verifier_key_id",
        "verifier_public_key_hash", "source_adapter_id",
    } <= columns
    with engine.connect() as db:
        after = db.execute(text(
            "SELECT receipt_id, request_id, issuer_id, charge_microusd, "
            "source_evidence_hash, attestation_digest, accepted_at, "
            "verification_scheme, verifier_key_id, verifier_public_key_hash, "
            "source_adapter_id FROM safe_inference_billing_attestations"
        )).mappings().one()
        assert {key: after[key] for key in before} == dict(before)
        assert after["verification_scheme"] == "legacy_hmac"
        assert after["verifier_key_id"] is None
        assert after["verifier_public_key_hash"] is None
        assert after["source_adapter_id"] == "legacy_hmac_v1"
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0016_episode_inference"
        )

    with pytest.raises(DBAPIError), engine.begin() as db:
        db.execute(text(
            "UPDATE safe_inference_billing_attestations "
            "SET verification_scheme = 'ed25519' WHERE receipt_id = :receipt_id"
        ), {"receipt_id": legacy["receipt_id"]})
    engine.dispose()
