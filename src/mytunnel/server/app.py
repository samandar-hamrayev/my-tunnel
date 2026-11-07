"""FastAPI relay: accepts tunnel clients over WebSocket and proxies public HTTP to them."""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from mytunnel import __version__
from mytunnel.config import ServerSettings
from mytunnel.headers import strip_hop_by_hop
from mytunnel.protocol import (
    PROTOCOL_VERSION,
    Error,
    Hello,
    HttpRequest,
    HttpResponse,
    ProtocolError,
    Registered,
    decode,
    encode,
    encode_body,
)
from mytunnel.server.registry import (
    NAME_RE,
    NameTakenError,
    Tunnel,
    TunnelBusyError,
    TunnelClosedError,
    TunnelRegistry,
)

log = logging.getLogger("mytunnel.server")

TUNNEL_WS_PATH = "/_tunnel"
PATH_PREFIX = "/t/"
PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

# WebSocket close codes (4000-4999 are reserved for applications).
WS_POLICY_VIOLATION = 1008
WS_NAME_TAKEN = 4409
WS_BAD_HANDSHAKE = 4400


class BodyTooLargeError(Exception):
    pass


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _bearer_token(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _token_ok(candidate: str | None, accepted: list[str]) -> bool:
    if candidate is None:
        return False
    # Compare against every token without short-circuiting: constant time per token.
    results = [hmac.compare_digest(candidate.encode(), t.encode()) for t in accepted]
    return any(results)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BodyTooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    """Build the relay application. Pass ``settings`` explicitly in tests."""
    settings = settings or ServerSettings()  # type: ignore[call-arg]  # filled from env
    registry = TunnelRegistry()
    accepted_tokens = settings.token_list

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("relay ready (protocol v%d, %s routing)", PROTOCOL_VERSION, _routing_mode())
        yield
        await registry.close_all()

    app = FastAPI(
        title="mytunnel relay",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry
    app.state.settings = settings

    def _routing_mode() -> str:
        return f"subdomain (*.{settings.domain})" if settings.domain else "path-prefix (/t/<name>/)"

    def _public_url(websocket: WebSocket, name: str) -> str:
        if settings.public_url:
            base = settings.public_url.rstrip("/")
            scheme, _, host = base.partition("://")
        else:
            scheme = "https" if websocket.url.scheme == "wss" else "http"
            host = websocket.headers.get("host", "localhost")
            base = f"{scheme}://{host}"
        if settings.domain:
            return f"{scheme}://{name}.{settings.domain}/"
        return f"{base}{PATH_PREFIX}{name}/"

    # ------------------------------------------------------------------ control

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "tunnels": len(registry)}

    @app.websocket(TUNNEL_WS_PATH)
    async def tunnel_endpoint(websocket: WebSocket) -> None:
        if not _token_ok(_bearer_token(websocket), accepted_tokens):
            # Closing before accept() makes the server answer the handshake with HTTP 403.
            log.warning("rejected tunnel client %s: bad token", websocket.client)
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        await websocket.accept()

        tunnel = await _handshake(websocket)
        if tunnel is None:
            return
        log.info("tunnel %s connected from %s", tunnel.name, websocket.client)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = decode(raw)
                except ProtocolError as exc:
                    log.warning("tunnel %s: %s", tunnel.name, exc)
                    continue
                if isinstance(message, HttpResponse):
                    tunnel.resolve(message)
                else:
                    log.warning("tunnel %s: unexpected %s frame", tunnel.name, message.type)
        except WebSocketDisconnect:
            pass
        finally:
            registry.unregister(tunnel)
            log.info("tunnel %s disconnected", tunnel.name)

    async def _handshake(websocket: WebSocket) -> Tunnel | None:
        async def reject(code: Error, close_code: int) -> None:
            await websocket.send_text(encode(code))
            await websocket.close(code=close_code)

        try:
            async with asyncio.timeout(settings.handshake_timeout):
                hello = decode(await websocket.receive_text())
        except WebSocketDisconnect:
            return None
        except (TimeoutError, ProtocolError):
            hello = None
        if not isinstance(hello, Hello):
            await reject(
                Error(code="bad_handshake", message="expected a hello frame"), WS_BAD_HANDSHAKE
            )
            return None
        if hello.version != PROTOCOL_VERSION:
            await reject(
                Error(
                    code="unsupported_version",
                    message=f"server speaks protocol v{PROTOCOL_VERSION}",
                ),
                WS_BAD_HANDSHAKE,
            )
            return None

        name = hello.subdomain.lower() if hello.subdomain else registry.generate_name()
        if not NAME_RE.fullmatch(name):
            await reject(
                Error(code="invalid_name", message="use 1-32 chars of a-z, 0-9 and '-'"),
                WS_BAD_HANDSHAKE,
            )
            return None

        tunnel = Tunnel(name, websocket, max_pending=settings.max_pending_per_tunnel)
        try:
            registry.register(tunnel)
        except NameTakenError:
            await reject(
                Error(code="name_taken", message=f"tunnel '{name}' is already in use"),
                WS_NAME_TAKEN,
            )
            return None
        await websocket.send_text(
            encode(Registered(tunnel=name, public_url=_public_url(websocket, name)))
        )
        return tunnel

    # -------------------------------------------------------------------- proxy

    def _resolve_target(request: Request) -> tuple[str, str] | None:
        """Map an incoming request to (tunnel name, upstream path)."""
        if settings.domain:
            host = request.headers.get("host", "").split(":", 1)[0].lower()
            suffix = f".{settings.domain}"
            if host.endswith(suffix):
                return host.removesuffix(suffix), request.url.path
        path = request.url.path
        if path.startswith(PATH_PREFIX):
            name, _, rest = path.removeprefix(PATH_PREFIX).partition("/")
            if name:
                return name, "/" + rest
        return None

    @app.api_route("/{_path:path}", methods=PROXY_METHODS, include_in_schema=False)
    async def proxy(request: Request, _path: str) -> Response:
        target = _resolve_target(request)
        if target is None:
            return _error(404, "no tunnel in this URL")
        name, upstream_path = target
        tunnel = registry.get(name)
        if tunnel is None:
            return _error(404, f"tunnel '{name}' not found")

        try:
            body = await _read_body(request, settings.max_body_bytes)
        except BodyTooLargeError:
            return _error(413, f"request body exceeds {settings.max_body_bytes} bytes")

        headers = strip_hop_by_hop(
            request.headers.items(), drop=("x-forwarded-for", "x-forwarded-proto")
        )
        client_ip = request.client.host if request.client else "unknown"
        headers += [
            ("x-forwarded-for", client_ip),
            ("x-forwarded-proto", request.url.scheme),
            ("x-forwarded-host", request.headers.get("host", "")),
        ]
        outgoing = HttpRequest(
            id=uuid.uuid4().hex,
            method=request.method,
            path=upstream_path,
            query=request.url.query,
            headers=headers,
            body=encode_body(body),
        )

        try:
            async with asyncio.timeout(settings.request_timeout):
                answer = await tunnel.forward(outgoing)
        except TimeoutError:
            return _error(504, "tunnel client did not answer in time")
        except TunnelClosedError:
            return _error(502, "tunnel client disconnected")
        except TunnelBusyError:
            return _error(503, "too many in-flight requests on this tunnel")

        try:
            payload = answer.body_bytes
        except ProtocolError:
            return _error(502, "tunnel client sent an invalid body")
        if len(payload) > settings.max_body_bytes:
            return _error(502, "response body exceeds the relay limit")

        response = Response(content=payload, status_code=answer.status)
        for key, value in strip_hop_by_hop(answer.headers, drop=("content-length",)):
            response.headers.append(key, value)
        return response

    return app
