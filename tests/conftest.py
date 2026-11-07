"""Shared fixtures: real uvicorn servers on ephemeral ports, all inside the test's event loop."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mytunnel.client import ClientConfig, TunnelClient
from mytunnel.config import ServerSettings
from mytunnel.protocol import Registered, encode_body
from mytunnel.server import create_app

TOKEN = "test-token"


@contextlib.asynccontextmanager
async def serve(app: object, **config: object) -> AsyncIterator[int]:
    """Run an ASGI app with uvicorn on 127.0.0.1:<ephemeral> and yield the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", **config))  # type: ignore[arg-type]
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # startup takes a few ms; there is no event to await
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    try:
        yield port
    finally:
        server.should_exit = True
        await task
        sock.close()


@dataclass
class Upstream:
    """A tiny local app that the tunnel forwards to, with hooks for tests."""

    port: int
    hang_started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    barrier: asyncio.Barrier | None = None


def build_upstream_app(state: Upstream) -> FastAPI:
    app = FastAPI()

    @app.api_route("/echo/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def echo(request: Request, rest: str) -> JSONResponse:
        body = await request.body()
        return JSONResponse(
            {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "headers": dict(request.headers.items()),
                "body": encode_body(body),
            }
        )

    @app.get("/status/{code}")
    async def status(code: int) -> Response:
        return Response(content=f"status {code}".encode(), status_code=code)

    @app.get("/cookies")
    async def cookies() -> Response:
        response = Response(content=b"ok", media_type="text/plain")
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    @app.get("/binary")
    async def binary() -> Response:
        return Response(content=bytes(range(256)), media_type="application/octet-stream")

    @app.get("/hang")
    async def hang() -> Response:
        state.hang_started.set()
        await state.release.wait()  # held until fixture teardown
        return Response(content=b"too late")

    @app.get("/together/{i}")
    async def together(i: int) -> dict[str, int]:
        # Only returns once every concurrent request has arrived: proves multiplexing.
        assert state.barrier is not None
        async with asyncio.timeout(5):
            await state.barrier.wait()
        return {"i": i}

    return app


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    state = Upstream(port=0)
    async with serve(build_upstream_app(state)) as port:
        state.port = port
        try:
            yield state
        finally:
            state.release.set()


ServerFactory = Callable[..., contextlib.AbstractAsyncContextManager[tuple[int, FastAPI]]]


@pytest.fixture
def make_server() -> ServerFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: object) -> AsyncIterator[tuple[int, FastAPI]]:
        settings = ServerSettings(tokens=TOKEN, **overrides)  # type: ignore[arg-type]
        app = create_app(settings)
        # proxy_headers=False: the relay is reached directly, not via a trusted proxy.
        async with serve(app, ws_max_size=settings.ws_max_size, proxy_headers=False) as port:
            yield port, app

    return factory


@pytest.fixture
async def relay(make_server: ServerFactory) -> AsyncIterator[tuple[int, FastAPI]]:
    async with make_server(request_timeout=1.0, max_body_bytes=64 * 1024) as running:
        yield running


@dataclass
class RunningClient:
    client: TunnelClient
    task: asyncio.Task[None]
    registrations: asyncio.Queue[Registered]

    async def registered(self) -> Registered:
        async with asyncio.timeout(5):
            return await self.registrations.get()


@contextlib.asynccontextmanager
async def start_client(
    server_port: int,
    local_port: int,
    *,
    token: str = TOKEN,
    subdomain: str | None = "demo",
) -> AsyncIterator[RunningClient]:
    registrations: asyncio.Queue[Registered] = asyncio.Queue()
    client = TunnelClient(
        ClientConfig(
            server=f"ws://127.0.0.1:{server_port}",
            token=token,
            local_url=f"http://127.0.0.1:{local_port}",
            subdomain=subdomain,
            request_timeout=5.0,
            reconnect_initial=0.01,
            reconnect_max=0.05,
            drain_timeout=0.2,
        ),
        on_registered=registrations.put_nowait,
    )
    task = asyncio.create_task(client.run())
    try:
        yield RunningClient(client, task, registrations)
    finally:
        client.stop()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            async with asyncio.timeout(5):
                await task


@pytest.fixture
async def tunnel(
    relay: tuple[int, FastAPI], upstream: Upstream
) -> AsyncIterator[tuple[httpx.AsyncClient, RunningClient]]:
    """A registered tunnel named 'demo' plus an HTTP client pointed at the relay."""
    port, _ = relay
    async with start_client(port, upstream.port) as running:
        await running.registered()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield http, running
