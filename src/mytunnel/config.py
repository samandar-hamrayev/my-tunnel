"""Server configuration, loaded from ``MYTUNNEL_*`` environment variables or ``.env``."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYTUNNEL_", env_file=".env", extra="ignore")

    tokens: SecretStr = Field(
        description="Comma-separated list of accepted client tokens. Required.",
    )
    host: str = "0.0.0.0"  # noqa: S104 - a relay is meant to listen publicly
    port: int = Field(default=8080, ge=1, le=65535)
    domain: str | None = Field(
        default=None,
        description="Enables subdomain routing: <name>.<domain> is proxied to tunnel <name>.",
    )
    public_url: str | None = Field(
        default=None,
        description="Externally visible base URL (e.g. https://tunnel.example.com).",
    )
    request_timeout: float = Field(default=30.0, gt=0)
    handshake_timeout: float = Field(default=10.0, gt=0)
    max_body_bytes: int = Field(default=10 * MIB, gt=0)
    max_pending_per_tunnel: int = Field(default=100, gt=0)
    trusted_proxies: str = Field(
        default="127.0.0.1",
        description="Comma-separated IPs/CIDRs whose X-Forwarded-* headers are trusted.",
    )
    log_level: str = "info"

    @field_validator("tokens")
    @classmethod
    def _tokens_not_empty(cls, value: SecretStr) -> SecretStr:
        if not [t for t in value.get_secret_value().split(",") if t.strip()]:
            raise ValueError("at least one token must be configured")
        return value

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, value: str | None) -> str | None:
        return value.strip().strip(".").lower() or None if value else None

    @property
    def token_list(self) -> list[str]:
        return [t.strip() for t in self.tokens.get_secret_value().split(",") if t.strip()]

    @property
    def ws_max_size(self) -> int:
        """Largest WebSocket frame accepted: a max-size body after base64 plus JSON envelope."""
        return self.max_body_bytes * 4 // 3 + 64 * 1024
