from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mytunnel import __version__
from mytunnel.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_http_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYTUNNEL_TOKEN", raising=False)
    result = runner.invoke(app, ["http", "3000"])
    assert result.exit_code != 0
    assert "token" in result.output


def test_http_rejects_bad_server_scheme() -> None:
    result = runner.invoke(app, ["http", "3000", "--token", "t", "--server", "ftp://relay"])
    assert result.exit_code != 0
    assert "scheme" in result.output


def test_server_fails_fast_without_tokens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MYTUNNEL_TOKENS", raising=False)
    monkeypatch.chdir(tmp_path)  # make sure no stray .env is picked up
    result = runner.invoke(app, ["server"])
    assert result.exit_code == 2
    assert "tokens" in result.output
