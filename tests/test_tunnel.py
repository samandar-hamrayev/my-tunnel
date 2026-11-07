"""End-to-end: public HTTP -> relay -> WebSocket -> client -> local app, and back."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from mytunnel.client import AuthenticationError, HandshakeError
from mytunnel.protocol import Error, decode, decode_body
from mytunnel.server.registry import TunnelRegistry

from .conftest import RunningClient, ServerFactory, Upstream, start_client

Tunnel = tuple[httpx.AsyncClient, RunningClient]


async def test_health(relay: tuple[int, FastAPI]) -> None:
    port, _ = relay
    async with httpx.AsyncClient() as http:
        response = await http.get(f"http://127.0.0.1:{port}/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_registration_reports_public_url(
    relay: tuple[int, FastAPI], upstream: Upstream
) -> None:
    port, app = relay
    async with start_client(port, upstream.port, subdomain="demo") as running:
        registered = await running.registered()
        assert registered.tunnel == "demo"
        assert registered.public_url == f"http://127.0.0.1:{port}/t/demo/"
        registry: TunnelRegistry = app.state.registry
        assert registry.names() == ["demo"]


async def test_random_name_when_none_requested(
    relay: tuple[int, FastAPI], upstream: Upstream
) -> None:
    port, _ = relay
    async with start_client(port, upstream.port, subdomain=None) as running:
        registered = await running.registered()
        assert len(registered.tunnel) == 8


async def test_get_with_query_and_headers(tunnel: Tunnel) -> None:
    http, _ = tunnel
    response = await http.get(
        "/t/demo/echo/items", params={"q": "tash kent", "page": "2"}, headers={"X-Trace": "abc"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "GET"
    assert data["path"] == "/echo/items"
    assert data["query"] == "q=tash+kent&page=2"
    assert data["headers"]["x-trace"] == "abc"
    assert data["headers"]["x-forwarded-for"] == "127.0.0.1"
    assert data["headers"]["x-forwarded-proto"] == "http"
    assert data["headers"]["host"].startswith("127.0.0.1:")  # rewritten to the local app


async def test_post_binary_body(tunnel: Tunnel) -> None:
    http, _ = tunnel
    payload = bytes(range(256)) * 10
    response = await http.post(
        "/t/demo/echo/upload",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "POST"
    assert decode_body(data["body"]) == payload
    assert data["headers"]["content-length"] == str(len(payload))


async def test_binary_response(tunnel: Tunnel) -> None:
    http, _ = tunnel
    response = await http.get("/t/demo/binary")
    assert response.status_code == 200
    assert response.content == bytes(range(256))
    assert response.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("code", [201, 204, 302, 404, 500])
async def test_status_codes_pass_through(tunnel: Tunnel, code: int) -> None:
    http, _ = tunnel
    response = await http.get(f"/t/demo/status/{code}")
    assert response.status_code == code


async def test_duplicate_response_headers_preserved(tunnel: Tunnel) -> None:
    http, _ = tunnel
    response = await http.get("/t/demo/cookies")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert {c.split(";")[0] for c in cookies} == {"a=1", "b=2"}


async def test_hop_by_hop_headers_not_forwarded(tunnel: Tunnel) -> None:
    http, _ = tunnel
    response = await http.get(
        "/t/demo/echo/",
        headers={
            "Connection": "keep-alive, X-Hop",
            "X-Hop": "must-not-leak",
            "Proxy-Authorization": "Basic Zm9vOmJhcg==",
            "X-Forwarded-For": "6.6.6.6",
        },
    )
    seen = response.json()["headers"]
    assert "x-hop" not in seen
    assert "proxy-authorization" not in seen
    assert seen["x-forwarded-for"] == "127.0.0.1"  # spoofed value replaced


async def test_concurrent_requests_are_multiplexed(tunnel: Tunnel, upstream: Upstream) -> None:
    """Each request blocks until all N have reached the local app.

    If the tunnel processed requests one at a time this would deadlock and time out.
    """
    http, _ = tunnel
    n = 20
    upstream.barrier = asyncio.Barrier(n)
    responses = await asyncio.gather(*(http.get(f"/t/demo/together/{i}") for i in range(n)))
    assert [r.status_code for r in responses] == [200] * n
    assert sorted(r.json()["i"] for r in responses) == list(range(n))


async def test_timeout_returns_504(tunnel: Tunnel, relay: tuple[int, FastAPI]) -> None:
    http, _ = tunnel
    _, app = relay
    response = await http.get("/t/demo/hang")
    assert response.status_code == 504
    assert app.state.registry.get("demo").pending_count == 0  # no leaked futures


async def test_client_disconnect_returns_502(tunnel: Tunnel, upstream: Upstream) -> None:
    http, running = tunnel
    pending = asyncio.create_task(http.get("/t/demo/hang"))
    await upstream.hang_started.wait()
    running.task.cancel()  # client dies abruptly mid-request
    response = await pending
    assert response.status_code == 502


async def test_unknown_tunnel_returns_404(tunnel: Tunnel) -> None:
    http, _ = tunnel
    assert (await http.get("/t/nope/anything")).status_code == 404
    assert (await http.get("/not-a-tunnel")).status_code == 404


async def test_request_body_limit_returns_413(tunnel: Tunnel) -> None:
    http, _ = tunnel
    response = await http.post("/t/demo/echo/", content=b"x" * (64 * 1024 + 1))
    assert response.status_code == 413


async def test_local_app_down_returns_502(relay: tuple[int, FastAPI]) -> None:
    port, _ = relay
    async with start_client(port, local_port=1) as running:  # nothing listens on port 1
        await running.registered()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            response = await http.get("/t/demo/")
    assert response.status_code == 502
    assert response.json() == {"error": "local app unreachable"}


async def test_bad_token_is_rejected(relay: tuple[int, FastAPI], upstream: Upstream) -> None:
    port, app = relay
    async with start_client(port, upstream.port, token="wrong") as running:
        with pytest.raises(AuthenticationError):
            await running.task
    assert len(app.state.registry) == 0


async def test_name_taken_is_fatal_for_new_client(tunnel: Tunnel, upstream: Upstream) -> None:
    http, _ = tunnel
    port = http.base_url.port
    assert port is not None
    async with start_client(port, upstream.port, subdomain="demo") as second:
        with pytest.raises(HandshakeError) as exc_info:
            await second.task
    assert exc_info.value.code == "name_taken"


async def test_invalid_name_is_rejected(relay: tuple[int, FastAPI], upstream: Upstream) -> None:
    port, _ = relay
    async with start_client(port, upstream.port, subdomain="Not_Valid!") as running:
        with pytest.raises(HandshakeError) as exc_info:
            await running.task
    assert exc_info.value.code == "invalid_name"


async def test_client_reconnects_after_server_drops_it(
    tunnel: Tunnel, relay: tuple[int, FastAPI]
) -> None:
    http, running = tunnel
    _, app = relay
    registry: TunnelRegistry = app.state.registry
    await registry.close_all(code=1012, reason="restart")  # simulate a relay restart
    await running.registered()  # client came back on its own
    response = await http.get("/t/demo/echo/again")
    assert response.status_code == 200


async def test_graceful_stop_unregisters(tunnel: Tunnel, relay: tuple[int, FastAPI]) -> None:
    _, running = tunnel
    _, app = relay
    running.client.stop()
    await running.task  # returns cleanly, no exception
    async with asyncio.timeout(5):
        # The relay unregisters after it processes the close frame, asynchronously.
        while len(app.state.registry):  # noqa: ASYNC110 - no hook to await here
            await asyncio.sleep(0.01)


async def test_subdomain_routing(make_server: ServerFactory, upstream: Upstream) -> None:
    async with (
        make_server(domain="tunnel.test", public_url="https://tunnel.test") as (port, _),
        start_client(port, upstream.port, subdomain="api") as running,
    ):
        registered = await running.registered()
        assert registered.public_url == "https://api.tunnel.test/"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            response = await http.get("/echo/x", headers={"Host": "api.tunnel.test"})
    assert response.status_code == 200
    assert response.json()["path"] == "/echo/x"


async def test_too_many_pending_returns_503(make_server: ServerFactory, upstream: Upstream) -> None:
    async with (
        make_server(max_pending_per_tunnel=1, request_timeout=5) as (port, _),
        start_client(port, upstream.port) as running,
    ):
        await running.registered()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            first = asyncio.create_task(http.get("/t/demo/hang"))
            await upstream.hang_started.wait()
            second = await http.get("/t/demo/echo/")
            assert second.status_code == 503
            upstream.release.set()
            assert (await first).status_code == 200


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ('{"type": "hello", "version": 999}', "unsupported_version"),
        ('{"type": "registered", "tunnel": "x", "public_url": "y"}', "bad_handshake"),
        ("garbage", "bad_handshake"),
    ],
)
async def test_handshake_rejections(relay: tuple[int, FastAPI], frame: str, code: str) -> None:
    port, _ = relay
    async with connect(
        f"ws://127.0.0.1:{port}/_tunnel", additional_headers={"Authorization": "Bearer test-token"}
    ) as ws:
        await ws.send(frame)
        reply = decode(await ws.recv())
        assert isinstance(reply, Error)
        assert reply.code == code
        with pytest.raises(ConnectionClosed):
            await ws.recv()
