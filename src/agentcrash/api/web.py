"""Same-origin delivery of the built web client (ADR-010) and the browser security headers.

The page and the API share one origin, so no CORS is configured. The CSP allows only this
origin: no inline script, no remote script, font, image or frame, and no form posts."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    b"content-security-policy": CSP.encode(),
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
}


class SecurityHeadersMiddleware:
    """Pure ASGI: the same headers on every HTTP response, API errors included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                own = message.get("headers", [])
                kept = [(k, v) for k, v in own if k.lower() not in SECURITY_HEADERS]
                message = {**message, "headers": [*kept, *SECURITY_HEADERS.items()]}
            await send(message)

        await self.app(scope, receive, send_secured)


def install_web(app: FastAPI, web_dir: Path) -> bool:
    """Serve ``web_dir/index.html`` at ``/`` and ``web_dir/assets`` under ``/assets``. Returns
    False (serving nothing) when the web client has not been built."""
    index = web_dir / "index.html"
    assets = web_dir / "assets"
    if not (index.is_file() and assets.is_dir()):
        return False

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    app.mount("/assets", StaticFiles(directory=assets), name="assets")
    return True
