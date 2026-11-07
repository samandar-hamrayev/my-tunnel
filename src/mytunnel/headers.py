"""HTTP header hygiene shared by the server and the client."""

from __future__ import annotations

from collections.abc import Iterable

# RFC 9110 section 7.6.1: connection-specific headers that a proxy must not forward.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def strip_hop_by_hop(
    headers: Iterable[tuple[str, str]],
    *,
    drop: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Return headers without hop-by-hop fields.

    Also removes any header listed in the ``Connection`` header itself
    (e.g. ``Connection: close, X-Foo`` makes ``X-Foo`` hop-by-hop) and any
    extra names passed in ``drop``. Names are compared case-insensitively;
    order and duplicates of the remaining headers are preserved.
    """
    pairs = list(headers)
    blocked = set(HOP_BY_HOP) | {name.lower() for name in drop}
    for name, value in pairs:
        if name.lower() == "connection":
            blocked.update(token.strip().lower() for token in value.split(",") if token.strip())
    return [(name, value) for name, value in pairs if name.lower() not in blocked]
