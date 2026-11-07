from __future__ import annotations

import json

import pytest

from mytunnel.client import ClientConfig, backoff_delay
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
    decode_body,
    encode,
    encode_body,
)


@pytest.mark.parametrize(
    "message",
    [
        Hello(subdomain="demo"),
        Hello(),
        Registered(tunnel="demo", public_url="http://relay/t/demo/"),
        Error(code="name_taken", message="taken"),
        HttpRequest(
            id="abc",
            method="POST",
            path="/api/items",
            query="a=1&b=2",
            headers=[("content-type", "application/json"), ("x-dup", "1"), ("x-dup", "2")],
            body=encode_body(b'{"x": 1}'),
        ),
        HttpResponse(id="abc", status=201, headers=[("set-cookie", "a=1")], body=""),
    ],
)
def test_roundtrip(message: object) -> None:
    raw = encode(message)  # type: ignore[arg-type]
    assert decode(raw) == message
    assert decode(raw.encode()) == message


def test_hello_defaults_to_current_version() -> None:
    assert json.loads(encode(Hello()))["version"] == PROTOCOL_VERSION


def test_duplicate_headers_preserved() -> None:
    req = HttpRequest(id="1", method="GET", path="/", headers=[("x", "1"), ("x", "2")])
    decoded = decode(encode(req))
    assert isinstance(decoded, HttpRequest)
    assert decoded.headers == [("x", "1"), ("x", "2")]


def test_binary_body_roundtrip() -> None:
    payload = bytes(range(256)) * 4
    resp = HttpResponse(id="1", status=200, body=encode_body(payload))
    decoded = decode(encode(resp))
    assert isinstance(decoded, HttpResponse)
    assert decoded.body_bytes == payload


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '{"type": "unknown"}',
        '{"type": "response", "id": "1", "status": 99}',
        '{"type": "request", "id": "1"}',
        '{"type": "hello", "version": 1, "extra": true}',
    ],
)
def test_decode_rejects_invalid_frames(raw: str) -> None:
    with pytest.raises(ProtocolError):
        decode(raw)


def test_decode_body_rejects_garbage() -> None:
    with pytest.raises(ProtocolError):
        decode_body("***")


def test_strip_hop_by_hop() -> None:
    headers = [
        ("Host", "example.com"),
        ("Connection", "keep-alive, X-Private"),
        ("Keep-Alive", "timeout=5"),
        ("Transfer-Encoding", "chunked"),
        ("X-Private", "secret"),
        ("Upgrade", "websocket"),
        ("Accept", "*/*"),
        ("Set-Cookie", "a=1"),
        ("Set-Cookie", "b=2"),
    ]
    assert strip_hop_by_hop(headers, drop=("host",)) == [
        ("Accept", "*/*"),
        ("Set-Cookie", "a=1"),
        ("Set-Cookie", "b=2"),
    ]


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("ws://relay:8080", "ws://relay:8080/_tunnel"),
        ("http://relay:8080/", "ws://relay:8080/_tunnel"),
        ("https://tunnel.example.com", "wss://tunnel.example.com/_tunnel"),
        ("wss://tunnel.example.com/custom", "wss://tunnel.example.com/custom"),
    ],
)
def test_client_ws_url(server: str, expected: str) -> None:
    config = ClientConfig(server=server, token="t", local_url="http://127.0.0.1:3000")
    assert config.ws_url == expected


def test_client_ws_url_rejects_unknown_scheme() -> None:
    config = ClientConfig(server="ftp://relay", token="t", local_url="http://x")
    with pytest.raises(ValueError, match="scheme"):
        _ = config.ws_url


def test_backoff_is_bounded_and_grows() -> None:
    for attempt in range(20):
        delay = backoff_delay(attempt, initial=0.5, maximum=30)
        ceiling = min(30, 0.5 * 2**attempt)
        assert ceiling / 2 <= delay <= ceiling
