from __future__ import annotations

import pytest
from pydantic import ValidationError

from mytunnel.config import ServerSettings


def test_tokens_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYTUNNEL_TOKENS", "alpha, beta,,")
    monkeypatch.setenv("MYTUNNEL_DOMAIN", ".Tunnel.Example.com.")
    settings = ServerSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.token_list == ["alpha", "beta"]
    assert settings.domain == "tunnel.example.com"
    assert "alpha" not in repr(settings)  # secrets are masked


def test_tokens_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYTUNNEL_TOKENS", raising=False)
    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ServerSettings(tokens=" , ", _env_file=None)  # type: ignore[call-arg]
