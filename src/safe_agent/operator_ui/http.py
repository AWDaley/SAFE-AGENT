"""Optional local operator UI; fixed packaged assets, never model-privileged."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

ASSET_DIR = Path(__file__).resolve().parent

SECURE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; font-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def create_operator_ui_router() -> APIRouter:
    """Serve only three named assets: no browsing server files or login bypass."""
    router = APIRouter(include_in_schema=False)

    @router.get("/operator")
    def home() -> HTMLResponse:
        return HTMLResponse(
            ASSET_DIR.joinpath("index.html").read_text(encoding="utf-8"),
            headers=SECURE_HEADERS,
        )

    @router.get("/operator/assets/app.js")
    def javascript() -> Response:
        return Response(
            ASSET_DIR.joinpath("app.js").read_text(encoding="utf-8"),
            media_type="text/javascript", headers=SECURE_HEADERS,
        )

    @router.get("/operator/assets/styles.css")
    def stylesheet() -> Response:
        return Response(
            ASSET_DIR.joinpath("styles.css").read_text(encoding="utf-8"),
            media_type="text/css", headers=SECURE_HEADERS,
        )

    return router
