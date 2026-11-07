"""Tunnel client: keeps a WebSocket open to the relay and forwards requests to a local app."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from mytunnel.headers import strip_hop_by_hop
from mytunnel.protocol import (
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

log = logging.getLogger("mytunnel.client")

MIB = 1024 * 1024
TUNNEL_WS_PATH = "/_tunnel"


class TunnelClientError(Exception):
    """A fatal error: retrying would not help (bad token, name taken, ...)."""


class AuthenticationError(TunnelClientError):
    pass


class HandshakeError(TunnelClientError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: str
    """Relay base URL: ws://, wss://, http:// or https://. ``/_tunnel`` is appended if no path."""
    token: str
    local_url: str
    """Where requests are forwarded, e.g. ``http://127.0.0.1:3000``."""
    subdomain: str | None = None
    request_timeout: float = 30.0
    max_body_bytes: int = 10 * MIB
    reconnect_initial: float = 0.5
    reconnect_max: float = 30.0
    handshake_timeout: float = 10.0
    drain_timeout: float = 5.0

    @property
    def ws_url(self) -> str:
        parts = urlsplit(self.server)
        scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
        if scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported server URL scheme: {parts.scheme!r}")
        path = parts.path.rstrip("/") or TUNNEL_WS_PATH
        return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


def backoff_delay(attempt: int, initial: float, maximum: float) -> float:
    """Exponential backoff with jitter: 50-100% of min(maximum, initial * 2**attempt)."""
    ceiling = min(maximum, initial * (2**attempt))
    return ceiling * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not crypto


class TunnelClient:
    def __init__(
        self,
        config: ClientConfig,
        *,
        on_registered: Callable[[Registered], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._on_registered = on_registered
        self._transport = transport
        self._stop = asyncio.Event()
        self._registered_once = False

    def stop(self) -> None:
        """Request a graceful shutdown: drain in-flight requests, then close the socket."""
        self._stop.set()

    async def run(self) -> None:
        """Connect, serve, and reconnect with backoff until :meth:`stop` or a fatal error."""
        attempt = 0
        async with httpx.AsyncClient(
            base_url=self.config.local_url,
            timeout=self.config.request_timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as http:
            while not self._stop.is_set():
                connected = False
                try:
                    connected = await self._session(http)
                except InvalidStatus as exc:
                    status = exc.response.status_code
                    if status in {401, 403}:
                        raise AuthenticationError("relay rejected the token") from exc
                    log.warning("relay refused the connection (HTTP %d)", status)
                except HandshakeError as exc:
                    # After a network blip the relay may not have noticed our old socket
                    # is dead yet, so our own name still looks taken: retry in that case.
                    if not (exc.code == "name_taken" and self._registered_once):
                        raise
                    log.warning("tunnel name still held by the previous session, retrying")
                except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake) as exc:
                    log.warning("connection to relay lost: %s", exc or type(exc).__name__)

                if self._stop.is_set():
                    break
                if connected:
                    attempt = 0
                delay = backoff_delay(
                    attempt, self.config.reconnect_initial, self.config.reconnect_max
                )
                attempt += 1
                log.info("reconnecting in %.1fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _session(self, http: httpx.AsyncClient) -> bool:
        """Run one WebSocket session. Returns True if the tunnel got registered."""
        async with connect(
            self.config.ws_url,
            additional_headers={"Authorization": f"Bearer {self.config.token}"},
            max_size=self.config.max_body_bytes * 4 // 3 + 64 * 1024,
            open_timeout=self.config.handshake_timeout,
        ) as ws:
            await ws.send(encode(Hello(subdomain=self.config.subdomain)))
            async with asyncio.timeout(self.config.handshake_timeout):
                first = decode(await ws.recv())
            if isinstance(first, Error):
                raise HandshakeError(first.code, first.message)
            if not isinstance(first, Registered):
                raise HandshakeError("bad_handshake", f"unexpected {first.type} frame")

            self._registered_once = True
            log.debug("tunnel online: %s -> %s", first.public_url, self.config.local_url)
            if self._on_registered is not None:
                self._on_registered(first)

            await self._serve(ws, http)
            return True

    async def _serve(self, ws: ClientConnection, http: httpx.AsyncClient) -> None:
        in_flight: set[asyncio.Task[None]] = set()
        send_lock = asyncio.Lock()

        async def reader() -> None:
            async for raw in ws:
                try:
                    message = decode(raw)
                except ProtocolError as exc:
                    log.warning("ignoring bad frame from relay: %s", exc)
                    continue
                if isinstance(message, HttpRequest):
                    task = asyncio.create_task(self._handle(ws, http, message, send_lock))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)

        read_task = asyncio.create_task(reader())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({read_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if stop_task.done():
                log.info("shutting down, draining %d in-flight request(s)", len(in_flight))
                read_task.cancel()
                if in_flight:
                    await asyncio.wait(in_flight, timeout=self.config.drain_timeout)
                await ws.close(code=1000, reason="client shutting down")
            else:
                read_task.result()  # re-raise ConnectionClosedError, if any
        finally:
            for task in (read_task, stop_task, *in_flight):
                task.cancel()

    async def _handle(
        self,
        ws: ClientConnection,
        http: httpx.AsyncClient,
        request: HttpRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        started = time.perf_counter()
        try:
            response = await self._forward(http, request)
        except Exception:
            log.exception("failed to forward %s %s", request.method, request.path)
            response = _error_response(request.id, 502, "tunnel client error")
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info("%s %s -> %d (%.0f ms)", request.method, request.path, response.status, elapsed_ms)
        with contextlib.suppress(ConnectionClosed):
            async with send_lock:
                await ws.send(encode(response))

    async def _forward(self, http: httpx.AsyncClient, request: HttpRequest) -> HttpResponse:
        headers = strip_hop_by_hop(request.headers, drop=("host", "content-length"))
        url = request.path + (f"?{request.query}" if request.query else "")
        outgoing = http.build_request(
            request.method, url, headers=headers, content=request.body_bytes
        )
        try:
            upstream = await http.send(outgoing, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                # Raw bytes: keep Content-Encoding intact instead of letting httpx decompress.
                async for chunk in upstream.aiter_raw():
                    size += len(chunk)
                    if size > self.config.max_body_bytes:
                        return _error_response(request.id, 502, "local response too large")
                    chunks.append(chunk)
            finally:
                await upstream.aclose()
        except httpx.TimeoutException:
            return _error_response(request.id, 504, "local app timed out")
        except httpx.TransportError as exc:
            log.warning("local app unreachable at %s: %s", self.config.local_url, exc)
            return _error_response(request.id, 502, "local app unreachable")

        return HttpResponse(
            id=request.id,
            status=upstream.status_code,
            headers=strip_hop_by_hop(upstream.headers.multi_items(), drop=("content-length",)),
            body=encode_body(b"".join(chunks)),
        )


def _error_response(request_id: str, status: int, message: str) -> HttpResponse:
    body = json.dumps({"error": message}).encode()
    return HttpResponse(
        id=request_id,
        status=status,
        headers=[("content-type", "application/json")],
        body=encode_body(body),
    )
