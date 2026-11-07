"""In-memory registry of live tunnels and their in-flight requests."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Protocol

from mytunnel.protocol import HttpRequest, HttpResponse, encode

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


class TunnelClosedError(Exception):
    """The client went away before answering."""


class TunnelBusyError(Exception):
    """Too many requests are already waiting on this tunnel."""


class NameTakenError(Exception):
    pass


class Sender(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class Tunnel:
    """One connected client. Multiplexes many HTTP requests over a single socket.

    Each forwarded request registers an ``asyncio.Future`` under its request id;
    the socket reader resolves it when the matching response frame arrives.
    """

    def __init__(self, name: str, socket: Sender, *, max_pending: int) -> None:
        self.name = name
        self.socket = socket
        self.max_pending = max_pending
        self._pending: dict[str, asyncio.Future[HttpResponse]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def forward(self, request: HttpRequest) -> HttpResponse:
        """Send a request to the client and wait for its response.

        Raises :class:`TunnelClosedError` or :class:`TunnelBusyError`. The caller bounds
        the wait with ``asyncio.timeout``; cancellation cleans up the pending entry.
        """
        if self._closed:
            raise TunnelClosedError(self.name)
        if len(self._pending) >= self.max_pending:
            raise TunnelBusyError(self.name)

        future: asyncio.Future[HttpResponse] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future
        try:
            try:
                async with self._send_lock:
                    await self.socket.send_text(encode(request))
            except Exception as exc:  # socket already gone
                raise TunnelClosedError(self.name) from exc
            return await future
        finally:
            self._pending.pop(request.id, None)

    def resolve(self, response: HttpResponse) -> None:
        future = self._pending.get(response.id)
        if future is None:
            # Late answer for a request that already timed out: nothing to do.
            log.debug("tunnel %s: dropping response for unknown id %s", self.name, response.id)
            return
        if not future.done():
            future.set_result(response)

    def fail_pending(self) -> None:
        """Mark the tunnel closed and fail every waiter with TunnelClosedError."""
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TunnelClosedError(self.name))


class TunnelRegistry:
    def __init__(self) -> None:
        self._tunnels: dict[str, Tunnel] = {}

    def __len__(self) -> int:
        return len(self._tunnels)

    def get(self, name: str) -> Tunnel | None:
        return self._tunnels.get(name)

    def names(self) -> list[str]:
        return sorted(self._tunnels)

    def register(self, tunnel: Tunnel) -> None:
        if tunnel.name in self._tunnels:
            raise NameTakenError(tunnel.name)
        self._tunnels[tunnel.name] = tunnel

    def unregister(self, tunnel: Tunnel) -> None:
        # Only remove the exact object: a reconnecting client may already own the name.
        if self._tunnels.get(tunnel.name) is tunnel:
            del self._tunnels[tunnel.name]
        tunnel.fail_pending()

    def generate_name(self) -> str:
        while True:
            name = secrets.token_hex(4)
            if name not in self._tunnels:
                return name

    async def close_all(self, code: int = 1001, reason: str = "server shutting down") -> None:
        tunnels = list(self._tunnels.values())
        self._tunnels.clear()
        for tunnel in tunnels:
            tunnel.fail_pending()
            try:
                await tunnel.socket.close(code=code, reason=reason)
            except Exception:  # best effort during shutdown
                log.debug("tunnel %s: close failed", tunnel.name, exc_info=True)
