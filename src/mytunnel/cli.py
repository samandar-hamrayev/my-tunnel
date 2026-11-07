"""Command-line interface: ``mytunnel http <port>`` and ``mytunnel server``."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

from mytunnel import __version__
from mytunnel.client import ClientConfig, TunnelClient, TunnelClientError
from mytunnel.config import ServerSettings
from mytunnel.protocol import Registered
from mytunnel.server import create_app

app = typer.Typer(
    name="mytunnel",
    help="Expose a local HTTP server through a self-hosted relay.",
    no_args_is_help=True,
    add_completion=False,
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # we log each request ourselves


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    pass


@app.command()
def http(
    port: Annotated[int, typer.Argument(help="Local port to expose.", min=1, max=65535)],
    server: Annotated[
        str, typer.Option(envvar="MYTUNNEL_SERVER", help="Relay URL, e.g. wss://tunnel.example.com")
    ] = "ws://127.0.0.1:8080",
    token: Annotated[
        str,
        typer.Option(envvar="MYTUNNEL_TOKEN", help="Auth token.", show_default=False),
    ] = "",
    subdomain: Annotated[
        str | None, typer.Option("--subdomain", "-s", help="Requested tunnel name.")
    ] = None,
    local_host: Annotated[
        str, typer.Option("--host", help="Local host to forward to.")
    ] = "127.0.0.1",
    timeout: Annotated[float, typer.Option(help="Local request timeout, seconds.")] = 30.0,
    log_level: Annotated[str, typer.Option(envvar="MYTUNNEL_LOG_LEVEL")] = "info",
) -> None:
    """Forward public HTTP traffic to http://HOST:PORT."""
    _setup_logging(log_level)
    if not token:
        raise typer.BadParameter("a token is required (--token or MYTUNNEL_TOKEN)")

    config = ClientConfig(
        server=server,
        token=token,
        local_url=f"http://{local_host}:{port}",
        subdomain=subdomain,
        request_timeout=timeout,
    )
    try:
        config.ws_url  # noqa: B018 - validate early
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    def announce(registered: Registered) -> None:
        typer.secho(
            f"Forwarding {registered.public_url} -> {config.local_url}",
            fg=typer.colors.GREEN,
            bold=True,
        )

    async def run() -> None:
        client = TunnelClient(config, on_registered=announce)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, client.stop)
        await client.run()

    try:
        asyncio.run(run())
    except TunnelClientError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def server(
    host: Annotated[str | None, typer.Option(help="Bind address (MYTUNNEL_HOST).")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port (MYTUNNEL_PORT).")] = None,
) -> None:
    """Run the relay server (configured via MYTUNNEL_* environment variables)."""
    try:
        settings = ServerSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        typer.secho(f"invalid configuration:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    _setup_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=host or settings.host,
        port=port or settings.port,
        ws_max_size=settings.ws_max_size,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        timeout_graceful_shutdown=10,
    )
