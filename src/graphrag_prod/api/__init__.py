"""Authenticated, bounded, observable production API boundary."""

from .app import APISettings, create_app
from .auth import AuthenticatedIdentity, JWTAuthConfig, JWTAuthenticator
from .backend import (
    GraphRAGApplicationBackend,
    GraphRAGQueryOperations,
    IncrementalIngestionPlanner,
    KnowledgeOperations,
    Neo4jDocumentOperations,
    ProviderUsage,
    QueryEmbedding,
)
from .knowledge import Neo4jKnowledgeOperations
from .resources import Neo4jResource, Neo4jSettings, create_neo4j_resource
from .runtime import (
    BackendResult,
    BoundedOperationRunner,
    OperationEnvelope,
    OperationKind,
    RateLimitPolicy,
    RuntimePolicy,
    UsageMetadata,
)

__all__ = [
    "APISettings",
    "AuthenticatedIdentity",
    "BackendResult",
    "BoundedOperationRunner",
    "GraphRAGApplicationBackend",
    "GraphRAGQueryOperations",
    "IncrementalIngestionPlanner",
    "KnowledgeOperations",
    "JWTAuthConfig",
    "JWTAuthenticator",
    "Neo4jResource",
    "Neo4jDocumentOperations",
    "Neo4jKnowledgeOperations",
    "Neo4jSettings",
    "OperationEnvelope",
    "OperationKind",
    "ProviderUsage",
    "QueryEmbedding",
    "RateLimitPolicy",
    "RuntimePolicy",
    "UsageMetadata",
    "create_app",
    "create_neo4j_resource",
]
