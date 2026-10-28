"""Independent, authenticated model-only sidecar for the benchmark profile.

Launch with ``python -m safe_agent.inference_service.episode_service``. It does
not import a business tool registry or receive the benchmark DB/evaluator.
The host must isolate its working directory/credentials from the planner.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from safe_agent.contracts.episode_inference import (
    EpisodeBackendCapabilities,
    EpisodeBackendRoute,
    EpisodeCapabilitiesReply,
    EpisodeCapabilitiesRequestEnvelope,
    EpisodeDispatchAuthenticator,
    EpisodeDispatchEnvelope,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    EpisodeServiceCapabilities,
    inference_failure_reason,
    strict_json,
)
from safe_agent.inference_service.__main__ import _secret_from_file
from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener


class ModelBackend(Protocol):
    def infer(self, request: EpisodeInferenceRequest) -> EpisodeInferenceReply: ...


class _ServiceInferenceFailure(ValueError):
    def __init__(self, reason_code: str):
        super().__init__("EPISODE_MODEL_OUTCOME_UNCERTAIN")
        self.reason_code = reason_code


async def _local_inference(inbound: Request, backend: ModelBackend,
                           request: EpisodeInferenceRequest) -> EpisodeInferenceReply:
    """Keep the dispatch fence when a caller leaves; close only its model call.

    The local backend owns a request-scoped socket and a finite total deadline.
    We wait for that bounded worker to finish before releasing service capacity;
    cancellation is not evidence that the model server performed no inference.
    """
    cancel = getattr(backend, "cancel", None)
    worker = asyncio.create_task(run_in_threadpool(backend.infer, request))
    disconnected = False
    try:
        while not worker.done():
            await asyncio.wait({worker}, timeout=0.05)
            if not disconnected and await inbound.is_disconnected():
                disconnected = True
                if callable(cancel):
                    cancel(request.request_id)
        try:
            reply = worker.result()
        except Exception:
            if disconnected:
                raise _ServiceInferenceFailure("EPISODE_CALLER_DISCONNECTED") from None
            raise
        if disconnected or await inbound.is_disconnected():
            raise _ServiceInferenceFailure("EPISODE_CALLER_DISCONNECTED")
        return reply
    except asyncio.CancelledError:
        if callable(cancel):
            cancel(request.request_id)
        try:
            await asyncio.shield(worker)
        except Exception:
            pass
        raise


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
    descriptor = getattr(backend, "capabilities", None)
    capabilities = None
    if isinstance(descriptor, EpisodeBackendCapabilities):
        if descriptor.model != model:
            raise ValueError("EPISODE_CAPABILITY_MODEL_CONFIGURATION_MISMATCH")
        capabilities = EpisodeServiceCapabilities(backend=descriptor, max_request_bytes=max_request_bytes)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ready", "mode": "episode_model_only"}

    @app.post("/v1/episode/capabilities")
    async def get_capabilities(inbound: Request) -> Response:
        """Authenticate public configuration without inference or fence writes."""
        headers = {"Cache-Control": "no-store"}
        if (inbound.headers.getlist("host") != ["safe-episode"]
                or inbound.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                or inbound.headers.get("content-encoding", "identity") != "identity"):
            return Response(status_code=400, headers=headers)
        raw = bytearray()
        try:
            async for chunk in inbound.stream():
                if len(raw) + len(chunk) > 8192:
                    return Response(status_code=413, headers=headers)
                raw.extend(chunk)
            strict_json(raw.decode("utf-8"), max_bytes=8192)
            envelope = EpisodeCapabilitiesRequestEnvelope.model_validate_json(bytes(raw))
            challenge = authenticator.verify_capabilities_request(envelope)
            now = clock()
            if challenge.model != model or not challenge.issued_at <= now < challenge.deadline:
                raise ValueError("capability route or deadline denied")
        except Exception:
            return Response(status_code=403, headers=headers)
        if capabilities is None:
            # Existing injected test backends without a descriptor retain their
            # inference behavior, but cannot claim configured hosted readiness.
            return Response(status_code=503, headers=headers)
        reply = EpisodeCapabilitiesReply(
            nonce=challenge.nonce, request_hash=challenge.digest,
            issued_at=now, deadline=challenge.deadline, capabilities=capabilities,
        )
        signed = authenticator.sign_capabilities_reply(reply)
        return Response(content=signed.model_dump_json(), media_type="application/json", headers=headers)

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
            profile = descriptor.local_timing if isinstance(descriptor, EpisodeBackendCapabilities) else None
            if profile is not None and (
                (request.deadline - request.issued_at).total_seconds() != profile.request_timeout_seconds
                or now >= request.issued_at + timedelta(seconds=profile.provider_timeout_seconds)
            ):
                raise _ServiceInferenceFailure("EPISODE_INFERENCE_LIFETIME_INSUFFICIENT")
        except Exception as exc:
            return Response(status_code=403, headers={
                **headers, "X-SAFE-Inference-Reason": inference_failure_reason(exc),
            })
        if not capacity.acquire(blocking=False):
            return Response(status_code=429, headers=headers)
        try:
            if not fence.claim(request):
                return Response(status_code=409, headers=headers)
            reply = (await _local_inference(inbound, backend, request) if profile is not None
                     else await run_in_threadpool(backend.infer, request))
            if clock() >= request.deadline:
                raise _ServiceInferenceFailure("EPISODE_INFERENCE_REPLY_LATE")
            if (reply.request_id != request.request_id or reply.input_hash != request.digest
                    or reply.model != model
                    or len(reply.model_dump_json().encode()) > 131_072):
                raise _ServiceInferenceFailure("EPISODE_MODEL_REPLY_INVALID")
            fence.finish(request, reply)
            return Response(content=reply.model_dump_json(), media_type="application/json",
                            headers=headers)
        except Exception as exc:
            # No error body/log contains raw provider errors, prompts, or keys.
            return Response(status_code=503, headers={
                **headers, "X-SAFE-Inference-Reason": inference_failure_reason(exc),
            })
        finally:
            capacity.release()

    return app


def backend_from_environment(*, model: str, dispatch_key: SecretStr) -> ModelBackend:
    """Select one route before startup; a local route never reads a hosted key."""
    route = EpisodeBackendRoute.model_validate({
        "kind": os.environ.get("SAFE_EPISODE_BACKEND", "hosted_openai"),
        "local_api_base": os.environ.get("SAFE_EPISODE_LOCAL_BASE_URL"),
        "local_timing": (strict_json(os.environ["SAFE_EPISODE_LOCAL_TIMING_JSON"], max_bytes=8192)
                         if "SAFE_EPISODE_LOCAL_TIMING_JSON" in os.environ else None),
    })
    if route.kind == "local_openai":
        from safe_agent.inference_service.providers.local_openai_episode import (
            LocalOpenAIEpisodeBackend,
        )

        assert route.local_api_base is not None
        return LocalOpenAIEpisodeBackend(model=model, base_url=route.local_api_base,
                                         local_timing=route.local_timing)
    from safe_agent.inference_service.providers.openai_episode import OpenAIEpisodeBackend

    provider_key = _secret_from_file(Path(os.environ["SAFE_EPISODE_OPENAI_KEY_FILE"]))
    if dispatch_key == provider_key:
        raise ValueError("SEPARATE_PROVIDER_AND_DISPATCH_KEYS_REQUIRED")
    return OpenAIEpisodeBackend(model=model, api_key=provider_key)


def main() -> None:
    import stat

    import uvicorn

    os.umask(0o077)
    root = Path(os.environ["SAFE_EPISODE_SERVICE_DIR"])
    info = root.lstat()
    if (not root.is_absolute() or root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("EPISODE_SERVICE_DIRECTORY_DENIED")
    key = _secret_from_file(Path(os.environ["SAFE_EPISODE_DISPATCH_KEY_FILE"]))
    model = os.environ["SAFE_EPISODE_MODEL"]
    backend = backend_from_environment(model=model, dispatch_key=key)
    app = create_episode_service(model=model, authenticator=EpisodeDispatchAuthenticator(key),
                                 backend=backend, fence=DurableServiceFence(root / "dispatch.sqlite"))
    with OwnedUnixListener(root / "episode.sock") as listener:
        uvicorn.run(app, fd=listener.fileno(), access_log=False, proxy_headers=False,
                    server_header=False, date_header=False, log_level="critical", lifespan="off")


if __name__ == "__main__":
    main()
