"""Wire protocol spoken between the relay server and the tunnel client.

Every frame is a single JSON text message on the WebSocket. Messages are
discriminated by their ``type`` field and validated with pydantic, so a
malformed or unknown frame is rejected at the boundary instead of deep inside
the proxy logic.

Handshake::

    client -> server   {"type": "hello", "version": 1, "subdomain": "demo"}
    server -> client   {"type": "registered", "tunnel": "demo", "public_url": "..."}
                   or  {"type": "error", "code": "name_taken", "message": "..."}

Traffic (multiplexed, many in flight at once, correlated by ``id``)::

    server -> client   {"type": "request", "id": "...", "method": "GET", ...}
    client -> server   {"type": "response", "id": "...", "status": 200, ...}

Bodies are base64-encoded so arbitrary binary payloads survive JSON.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1

Headers = list[tuple[str, str]]
"""Ordered header pairs. A list (not a dict) keeps duplicates such as Set-Cookie."""


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded into a known message."""


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Hello(_Message):
    """First frame sent by the client after the WebSocket is accepted."""

    type: Literal["hello"] = "hello"
    version: int = PROTOCOL_VERSION
    subdomain: str | None = None


class Registered(_Message):
    """Server confirmation that the tunnel is live."""

    type: Literal["registered"] = "registered"
    tunnel: str
    public_url: str


class Error(_Message):
    """Server-side rejection of the handshake. The server closes the socket after it."""

    type: Literal["error"] = "error"
    code: Literal["name_taken", "invalid_name", "unsupported_version", "bad_handshake"]
    message: str


class HttpRequest(_Message):
    """A public HTTP request, forwarded by the server to the client."""

    type: Literal["request"] = "request"
    id: str
    method: str
    path: str
    query: str = ""
    headers: Headers = Field(default_factory=list)
    body: str = ""

    @property
    def body_bytes(self) -> bytes:
        return decode_body(self.body)


class HttpResponse(_Message):
    """The local app's response, sent back by the client."""

    type: Literal["response"] = "response"
    id: str
    status: int = Field(ge=100, le=599)
    headers: Headers = Field(default_factory=list)
    body: str = ""

    @property
    def body_bytes(self) -> bytes:
        return decode_body(self.body)


Message = Annotated[
    Hello | Registered | Error | HttpRequest | HttpResponse,
    Field(discriminator="type"),
]

_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def encode(message: _Message) -> str:
    """Serialize a message to a JSON text frame."""
    return message.model_dump_json()


def decode(raw: str | bytes) -> Message:
    """Parse and validate a JSON frame. Raises :class:`ProtocolError` on any problem."""
    try:
        return _adapter.validate_json(raw)
    except ValidationError as exc:
        raise ProtocolError(f"invalid frame: {exc.error_count()} validation error(s)") from exc


def encode_body(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_body(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("body is not valid base64") from exc
