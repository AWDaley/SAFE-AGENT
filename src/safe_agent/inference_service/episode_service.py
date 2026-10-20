"""Independent, authenticated model-only sidecar for the benchmark profile.

Launch with ``python -m safe_agent.inference_service.episode_service``. It does
not import a business tool registry or receive the benchmark DB/evaluator.
The host must isolate its working directory/credentials from the planner.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from safe_agent.contracts.episode_inference import (
    EpisodeDispatchAuthenticator,
    EpisodeDispatchEnvelope,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    strict_json,
)
from safe_agent.inference_service.__main__ import _secret_from_file
from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener


class ModelBackend(Protocol):
    def infer(self, request: EpisodeInferenceRequest) -> EpisodeInferenceReply: ...


class DurableServiceFence:
    """A restarted process cannot repeat a claimed provider attempt."""

    def __init__(self, path: Path):
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests ("
                       "request_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, "
                       "reply_json TEXT, reply_hash TEXT)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def claim(self, request: EpisodeInferenceRequest) -> bool:
        with self._connect() as db:
            try:
                db.execute("INSERT INTO requests(request_id,input_hash) VALUES (?,?)",
                           (request.request_id, request.digest))
                return True
            except sqlite3.IntegrityError:
                return False

    def finish(self, request: EpisodeInferenceRequest, reply: EpisodeInferenceReply) -> None:
        with self._connect() as db:
            changed = db.execute("UPDATE requests SET reply_json=?,reply_hash=? "
                                 "WHERE request_id=? AND input_hash=? AND reply_json IS NULL",
                                 (reply.model_dump_json(), reply.digest,
                                  request.request_id, request.digest))
            if changed.rowcount != 1:
                raise ValueError("SERVICE_RESULT_BINDING_FAILED")


def create_episode_service(*, model: str, authenticator: EpisodeDispatchAuthenticator,
                           backend: ModelBackend, fence: DurableServiceFence,
                           max_request_bytes: int = 262_144,
                           clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    capacity = threading.BoundedSemaphore(1)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ready", "mode": "episode_model_only"}

    @app.post("/v1/episode/infer")
    async def infer(inbound: Request) -> Response:
        headers = {"Cache-Control": "no-store"}
        if (inbound.headers.getlist("host") != ["safe-episode"]
                or inbound.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                or inbound.headers.get("content-encoding", "identity") != "identity"):
            return Response(status_code=400, headers=headers)
        raw = bytearray()
        try:
            async for chunk in inbound.stream():
                if len(raw) + len(chunk) > max_request_bytes:
                    return Response(status_code=413, headers=headers)
                raw.extend(chunk)
            strict_json(raw.decode("utf-8"), max_bytes=max_request_bytes)
            envelope = EpisodeDispatchEnvelope.model_validate_json(bytes(raw))
            request = authenticator.verify(envelope)
            now = clock()
            if (request.model != model or not request.issued_at <= now < request.deadline
                    or (request.deadline - request.issued_at).total_seconds() > 120):
                raise ValueError("route or deadline denied")
        except Exception:
            return Response(status_code=403, headers=headers)
        if not capacity.acquire(blocking=False):
            return Response(status_code=429, headers=headers)
        try:
            if not fence.claim(request):
                return Response(status_code=409, headers=headers)
            reply = await run_in_threadpool(backend.infer, request)
            if (reply.request_id != request.request_id or reply.input_hash != request.digest
                    or reply.model != model or clock() >= request.deadline
                    or len(reply.model_dump_json().encode()) > 131_072):
                raise ValueError("invalid provider result")
            fence.finish(request, reply)
            return Response(content=reply.model_dump_json(), media_type="application/json",
                            headers=headers)
        except Exception:
            # No error body/log contains raw provider errors, prompts, or keys.
            return Response(status_code=503, headers=headers)
        finally:
            capacity.release()

    return app


def main() -> None:
    import stat

    import uvicorn

    from safe_agent.inference_service.providers.openai_episode import OpenAIEpisodeBackend

    os.umask(0o077)
    root = Path(os.environ["SAFE_EPISODE_SERVICE_DIR"])
    info = root.lstat()
    if (not root.is_absolute() or root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("EPISODE_SERVICE_DIRECTORY_DENIED")
    key = _secret_from_file(Path(os.environ["SAFE_EPISODE_DISPATCH_KEY_FILE"]))
    provider_key = _secret_from_file(Path(os.environ["SAFE_EPISODE_OPENAI_KEY_FILE"]))
    if key == provider_key:
        raise ValueError("SEPARATE_PROVIDER_AND_DISPATCH_KEYS_REQUIRED")
    model = os.environ["SAFE_EPISODE_MODEL"]
    backend = OpenAIEpisodeBackend(model=model, api_key=provider_key)
    app = create_episode_service(model=model, authenticator=EpisodeDispatchAuthenticator(key),
                                 backend=backend, fence=DurableServiceFence(root / "dispatch.sqlite"))
    with OwnedUnixListener(root / "episode.sock") as listener:
        uvicorn.run(app, fd=listener.fileno(), access_log=False, proxy_headers=False,
                    server_header=False, date_header=False, log_level="critical", lifespan="off")


if __name__ == "__main__":
    main()
