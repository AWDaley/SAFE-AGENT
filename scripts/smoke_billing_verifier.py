"""Actual-process smoke for the isolated billing verifier over a private UDS."""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from safe_agent.billing import SyntheticBillingSourceRecord
from safe_agent.billing.uds_verifier import BillingVerifierUDSTransport
from safe_agent.canonical import canonical_json
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation

HOST = "billing.example.com"
ISSUER = "independent_offline_billing_ed25519_v1"
KEY_ID = "billing_ed25519_smoke_v1"


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise RuntimeError("short private-file write")
        os.fsync(fd)
    finally:
        os.close(fd)


def _source_record(source_key: bytes) -> SyntheticBillingSourceRecord:
    initial = SyntheticBillingSourceRecord(
        source_record_id="src_" + "a" * 32,
        request_id="inf_" + "b" * 32,
        organization_id="org_smoke",
        task_id="task_smoke",
        route_id="offline_route",
        model_revision="offline_v1",
        prompt_digest="sha256:" + "c" * 64,
        charge_microusd=17,
        issued_at=datetime.now(UTC) - timedelta(seconds=1),
        source_signature="0" * 64,
    )
    payload = initial.model_dump(mode="json", exclude={"source_signature"})
    signature = hmac.new(source_key, canonical_json(payload), hashlib.sha256).hexdigest()
    return initial.model_copy(update={"source_signature": signature})


def main() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with tempfile.TemporaryDirectory(prefix="safe-billing-") as raw:
        root = Path(raw)
        os.chmod(root, 0o700)
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        socket_path = socket_dir / "billing.sock"
        token = ("t" * 64).encode("ascii")
        source_key = b"S" * 64
        private_obj = Ed25519PrivateKey.generate()
        private_key = private_obj.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_key = private_obj.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        token_file = root / "workload.token"
        source_file = root / "source.key"
        private_file = root / "issuer.key"
        _write(token_file, token)
        _write(source_file, source_key)
        _write(private_file, private_key)

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "safe_agent.billing_service",
                "--socket", str(socket_path),
                "--host", HOST,
                "--workload-token-file", str(token_file),
                "--source-key-file", str(source_file),
                "--issuer-private-key-file", str(private_file),
                "--issuer-id", ISSUER,
                "--verifier-key-id", KEY_ID,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("billing verifier exited before socket became ready")
                try:
                    info = socket_path.lstat()
                    if stat.S_ISSOCK(info.st_mode):
                        break
                except FileNotFoundError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("billing verifier socket not ready")

            transport = BillingVerifierUDSTransport(
                socket_path, expected_host=HOST,
                workload_token=SecretStr(token.decode("ascii")),
                timeout_seconds=2.0,
            )
            attestation = transport.verify(_source_record(source_key))
            checked = VerifiedBillingAttestation.model_validate_json(
                attestation.model_dump_json(),
            )
            payload = canonical_json(checked.model_dump(mode="json", exclude={"signature"}))
            private_obj.public_key().verify(bytes.fromhex(checked.signature), payload)
            if (checked.issuer_id, checked.verifier_key_id, checked.charge_microusd) != (
                ISSUER, KEY_ID, 17,
            ):
                raise RuntimeError("billing verifier response binding mismatch")
            if len(public_key) != 32:
                raise RuntimeError("unexpected Ed25519 public key size")
            print("ASYMMETRIC_BILLING_VERIFIER_UDS=PASS")
            print("DATABASE_ACCESS=NONE; BUSINESS_EFFECTS=0; LIVE_BILLING_SOURCE=DISABLED")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            if proc.returncode not in (0, -15):
                diagnostic = (proc.stderr.read() if proc.stderr else "")[:400]
                raise RuntimeError("billing verifier shutdown failed: " + diagnostic)


if __name__ == "__main__":
    main()
