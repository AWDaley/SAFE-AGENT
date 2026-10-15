"""Launch an inference-only ASGI service over a local Unix-domain socket.

No TCP listener, model provider, SQL, broker, OAuth credential or Google tool
is configured by this process. A separately reviewed TLS/mTLS proxy should
forward only the fixed /v1/infer route to the socket. Production OS sandbox
and operational alerting must be configured by the host administrator.
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

from fastapi import FastAPI
from pydantic import SecretStr

from safe_agent.inference_service.service import (
    TrustedInferenceServicePolicy,
    TrustedWorkloadAuthenticator,
    create_inference_service,
)


def _secret_from_file(path: Path) -> SecretStr:
    if not path.is_absolute():
        raise ValueError("inference workload token file must use an absolute path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise ValueError("inference workload token permissions denied")
        contents = os.read(descriptor, 513)
        if not 32 <= len(contents) <= 512:
            raise ValueError("inference workload token length denied")
        return SecretStr(contents.decode("ascii", errors="strict"))
    finally:
        os.close(descriptor)


def bootstrap() -> tuple[FastAPI, Path]:
    """Create the independent app from trusted service-host environment only."""
    required = (
        "SAFE_INFER_SOCKET_DIR",
        "SAFE_INFER_WORKLOAD_TOKEN_FILE",
        "SAFE_INFER_HOST",
        "SAFE_INFER_ROUTE",
        "SAFE_INFER_MODEL_REVISION",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError("required inference sidecar configuration missing")
    socket_dir = Path(os.environ["SAFE_INFER_SOCKET_DIR"])
    if not socket_dir.is_absolute() or socket_dir.is_symlink():
        raise ValueError("inference socket directory must be an absolute owned path")
    metadata = socket_dir.stat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise ValueError("inference socket directory permission denied")
    socket_file = socket_dir / "inference.sock"
    if socket_file.exists() or socket_file.is_symlink():
        raise ValueError("refusing to overwrite existing inference socket")
    auth = TrustedWorkloadAuthenticator(
        _secret_from_file(Path(os.environ["SAFE_INFER_WORKLOAD_TOKEN_FILE"])),
    )
    policy = TrustedInferenceServicePolicy(
        expected_host=os.environ["SAFE_INFER_HOST"],
        route_id=os.environ["SAFE_INFER_ROUTE"],
        model_revision=os.environ["SAFE_INFER_MODEL_REVISION"],
    )
    return create_inference_service(policy, auth), socket_file


def main() -> None:
    # This process never opens a TCP socket or a remote model connection.
    # Force owner-only creation of the Unix socket.
    os.umask(0o077)
    app, socket_path = bootstrap()
    import uvicorn

    # Bind and permission the Unix listener BEFORE Uvicorn accepts any
    # connection. Uvicorn's default UDS mode is not an authorization boundary.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(32)
        uvicorn.run(
            app, fd=listener.fileno(),
            access_log=False, proxy_headers=False,
            server_header=False, date_header=False,
            log_level="warning", lifespan="off",
        )
    finally:
        listener.close()
        if socket_path.is_socket():
            socket_path.unlink()


if __name__ == "__main__":
    main()
