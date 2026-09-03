"""Local browser playground for the validated GraphRAG retrieval core."""

from .runtime import (
    PLAYGROUND_AUDIENCE,
    PLAYGROUND_ISSUER,
    PLAYGROUND_RETRIEVAL_LIMITS,
    PLAYGROUND_TOKEN_LIFETIME_SECONDS,
    FixtureQueryEmbedder,
    PlaygroundCatalog,
    attach_playground_routes,
    require_loopback_host,
)

__all__ = [
    "PLAYGROUND_AUDIENCE",
    "PLAYGROUND_ISSUER",
    "PLAYGROUND_RETRIEVAL_LIMITS",
    "PLAYGROUND_TOKEN_LIFETIME_SECONDS",
    "FixtureQueryEmbedder",
    "PlaygroundCatalog",
    "attach_playground_routes",
    "require_loopback_host",
]
