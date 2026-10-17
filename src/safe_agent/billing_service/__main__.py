"""Run the isolated billing verifier over one owner-only Unix socket.

This process intentionally has no application database URL, broker key, operator
session key, provider-writing adapter or inference-dispatch credential.
"""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from pydantic import SecretStr

from safe_agent.billing.adapters import SyntheticHMACBillingAdapter
from safe_agent.billing_service.service import (
    TrustedBillingVerifierPolicy,
    TrustedBillingWorkloadAuthenticator,
    create_billing_verifier_service,
)
from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener


def _private_file(path: Path, *, minimum: int, maximum: int) -> bytes:
    if not path.is_absolute():
        raise ValueError("absolute private key path required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not minimum <= info.st_size <= maximum):
            raise ValueError("private key file denied")
        data = os.read(fd, maximum + 1)
        if not minimum <= len(data) <= maximum:
            raise ValueError("private key file denied")
        return data
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE-AGENT isolated billing verifier")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--workload-token-file", required=True, type=Path)
    parser.add_argument("--source-key-file", required=True, type=Path)
    parser.add_argument("--issuer-private-key-file", required=True, type=Path)
    parser.add_argument("--issuer-id", required=True)
    parser.add_argument("--verifier-key-id", required=True)
    args = parser.parse_args()
    socket_path = args.socket
    if (not socket_path.is_absolute() or len(os.fsencode(socket_path)) > 103):
        raise SystemExit("billing verifier socket path denied")
    try:
        token = _private_file(args.workload_token_file, minimum=32, maximum=512)
        source_key = _private_file(args.source_key_file, minimum=32, maximum=128)
        private_key = _private_file(args.issuer_private_key_file, minimum=32, maximum=32)
        workload = SecretStr(token.decode("ascii"))
        policy = TrustedBillingVerifierPolicy(
            expected_host=args.host, issuer_id=args.issuer_id,
            verifier_key_id=args.verifier_key_id,
        )
        app = create_billing_verifier_service(
            policy, TrustedBillingWorkloadAuthenticator(workload),
            adapter=SyntheticHMACBillingAdapter(
                source_key=source_key,
                max_evidence_age_seconds=policy.max_evidence_age_seconds,
            ),
            issuer_private_key=private_key,
        )
    except Exception:
        raise SystemExit("billing verifier startup denied") from None

    import uvicorn

    with OwnedUnixListener(socket_path) as listener:
        uvicorn.run(
            app, fd=listener.fileno(), access_log=False, proxy_headers=False,
            server_header=False, date_header=False, log_level="warning", lifespan="off",
        )


if __name__ == "__main__":
    main()
