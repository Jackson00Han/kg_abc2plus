"""Safe local-only adapters and routes for the retrieval Playground.

The Playground deliberately reuses the authenticated production API.  Routes
under ``/playground`` only supply public synthetic corpus metadata and short-
lived test identities; every retrieval still crosses ``/v1`` and therefore
the normal JWT, tenant, group, rate-limit, and bounded-runtime controls.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from importlib.resources import files
import ipaddress
import re
import time
from typing import Any, Mapping, Protocol

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, RedirectResponse
import jwt
from pydantic import BaseModel, ConfigDict, Field

from graphrag_prod.api.backend import ProviderUsage, QueryEmbedding
from graphrag_prod.retrieval import RetrievalLimits

PLAYGROUND_ISSUER = "sample-graphrag-local-playground"
PLAYGROUND_AUDIENCE = "sample-graphrag-local-api"
PLAYGROUND_TOKEN_LIFETIME_SECONDS = 900
PLAYGROUND_SCOPES = (
    "retrieval:read",
    "ontology:read",
    "ontology:write",
    "ontology:publish",
    "knowledge:import",
    "knowledge:construct",
    "knowledge:review",
    "knowledge:publish",
)
PLAYGROUND_RETRIEVAL_LIMITS = RetrievalLimits(
    top_k=5,
    seed_k=3,
    graph_entities_per_seed=8,
    graph_edges_per_seed=40,
    graph_candidates_per_seed=8,
    candidate_limit=50,
    anchor_k=3,
    minimum_vector_score=0.75,
)
_PERSONA_ID = re.compile(r"^persona-[0-9]{2}$")

# This is a visible, editable starting point only.  The Playground never
# imports or publishes it implicitly: a human must perform both API actions.
DEFAULT_INDUSTRIAL_TBOX_TEMPLATE: dict[str, Any] = {
    "key": "industrial-assets",
    "version": 1,
    "description": (
        "Starter property-graph ontology for governed industrial asset knowledge"
    ),
    "entity_types": [
        {
            "name": "Organization",
            "canonical_key_namespaces": ["organization-id", "llm-candidate"],
            "properties": [],
            "identity_properties": [],
            "description": "Operator, supplier, manufacturer, or service company",
        },
        {
            "name": "Site",
            "canonical_key_namespaces": ["site-id", "llm-candidate"],
            "properties": [],
            "identity_properties": [],
            "description": "Physical plant, facility, line, or operating location",
        },
        {
            "name": "Equipment",
            "canonical_key_namespaces": ["equipment-id", "llm-candidate"],
            "properties": [
                {
                    "name": "RatedPower",
                    "datatype": "DECIMAL",
                    "required": False,
                    "cardinality": "ZERO_OR_ONE",
                    "unit": "kW",
                    "description": "Nameplate rated power in kilowatts",
                }
            ],
            "identity_properties": [],
            "description": "Maintainable industrial equipment or machine",
        },
        {
            "name": "Component",
            "canonical_key_namespaces": ["component-id", "llm-candidate"],
            "properties": [],
            "identity_properties": [],
            "description": "Replaceable component belonging to equipment",
        },
        {
            "name": "Risk",
            "canonical_key_namespaces": ["risk-id", "llm-candidate"],
            "properties": [],
            "identity_properties": [],
            "description": "Operational, safety, supply, or reliability risk",
        },
    ],
    "relationship_types": [
        {
            "name": "OPERATES",
            "source_types": ["Organization"],
            "target_types": ["Site", "Equipment"],
            "description": "Organization operates a site or equipment",
        },
        {
            "name": "INSTALLED_AT",
            "source_types": ["Equipment", "Component"],
            "target_types": ["Site"],
            "description": "Asset is installed at a governed site",
        },
        {
            "name": "CONTAINS",
            "source_types": ["Equipment"],
            "target_types": ["Component"],
            "description": "Equipment contains a component",
        },
        {
            "name": "SUPPLIED_BY",
            "source_types": ["Equipment", "Component"],
            "target_types": ["Organization"],
            "description": "Asset is supplied by an organization",
        },
        {
            "name": "EXPOSED_TO",
            "source_types": ["Organization", "Site", "Equipment", "Component"],
            "target_types": ["Risk"],
            "description": "Industrial subject is exposed to a stated risk",
        },
    ],
}


class DevelopmentCorpus(Protocol):
    """Minimal interface supplied by the versioned development fixture."""

    build: Any
    vectors_by_id: Mapping[str, tuple[float, ...]]

    def query_vector(self, question: Mapping[str, Any]) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class PlaygroundPersona:
    persona_id: str
    principal_id: str
    label: str
    tenant_id: str
    groups: tuple[str, ...]
    scopes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.persona_id,
            "label": self.label,
            "tenant_id": self.tenant_id,
            "groups": list(self.groups),
            "scopes": list(self.scopes),
        }


def require_loopback_host(host: str) -> str:
    """Accept only an explicit loopback address for the token-issuing demo."""

    if not isinstance(host, str) or not host.strip():
        raise ValueError("Playground host must be an explicit loopback address")
    normalized = host.strip()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as error:
        raise ValueError(
            "Playground host must be 127.0.0.1 or ::1; hostnames are not accepted"
        ) from error
    if not address.is_loopback:
        raise ValueError("Playground must bind to a loopback address")
    return normalized


def _persona_label(tenant_id: str, groups: tuple[str, ...]) -> str:
    tenant = tenant_id.removeprefix("tenant-").replace("-", " ").title()
    names = [group.split("-", 1)[-1].replace("-", " ").title() for group in groups]
    return f"Tenant {tenant} · {' + '.join(names)}"


def _persona_scopes(tenant_id: str, groups: tuple[str, ...]) -> tuple[str, ...]:
    """Assign local demo duties without weakening production-style RBAC."""

    selected = {"retrieval:read", "ontology:read"}
    group_set = frozenset(groups)
    is_alpha_steward = tenant_id == "tenant-alpha" and {
        "alpha-finance",
        "alpha-legal",
    }.issubset(group_set)
    is_beta_steward = tenant_id == "tenant-beta" and "beta-board" in group_set
    if is_alpha_steward or is_beta_steward:
        selected.update(PLAYGROUND_SCOPES)
    elif "alpha-finance" in group_set:
        selected.add("knowledge:construct")
    elif "alpha-legal" in group_set:
        selected.add("knowledge:review")
    return tuple(scope for scope in PLAYGROUND_SCOPES if scope in selected)


class PlaygroundCatalog:
    """Expose only public synthetic fixtures and issue bounded local JWTs."""

    def __init__(
        self,
        fixture: DevelopmentCorpus,
        signing_key: bytes,
        *,
        embedding_metadata: Mapping[str, Any] | None = None,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("Playground signing key must contain at least 32 bytes")
        self.fixture = fixture
        self._signing_key = signing_key
        self._embedding_metadata = (
            dict(embedding_metadata) if embedding_metadata is not None else None
        )
        self._capabilities = dict(capabilities or {})
        raw_questions = tuple(fixture.build.questions)
        if not raw_questions:
            raise ValueError("Playground corpus must contain questions")

        identities = sorted(
            {
                (
                    str(question["principal"]["tenant_id"]),
                    tuple(sorted(str(value) for value in question["principal"]["groups"])),
                )
                for question in raw_questions
            }
        )
        self.personas = tuple(
            PlaygroundPersona(
                persona_id=f"persona-{index:02d}",
                principal_id=f"local-playground-{index:02d}",
                label=_persona_label(tenant_id, groups),
                tenant_id=tenant_id,
                groups=groups,
                scopes=_persona_scopes(tenant_id, groups),
            )
            for index, (tenant_id, groups) in enumerate(identities, start=1)
        )
        self.personas_by_id = {item.persona_id: item for item in self.personas}
        persona_id_by_scope = {
            (item.tenant_id, item.groups): item.persona_id for item in self.personas
        }

        questions: list[dict[str, Any]] = []
        for raw in raw_questions:
            principal = raw["principal"]
            scope = (
                str(principal["tenant_id"]),
                tuple(sorted(str(value) for value in principal["groups"])),
            )
            questions.append(
                {
                    "id": str(raw["id"]),
                    "query": str(raw["query"]),
                    "question_class": str(raw["question_class"]),
                    "case_type": str(raw["case_type"]),
                    "answerable": bool(raw["answerable"]),
                    "recommended_persona_id": persona_id_by_scope[scope],
                }
            )
        self.questions = tuple(questions)
        self.questions_by_id = {item["id"]: item for item in self.questions}

    def bootstrap(self) -> dict[str, Any]:
        manifest = self.fixture.build.manifest
        profile = manifest["embedding_profile"]
        capabilities = {
            "reviewed_questions": True,
            "custom_semantic_retrieval": self._embedding_metadata is not None,
            "custom_bm25_retrieval": True,
            "document_upload": False,
            "ontology_governance": False,
            "human_review": False,
            "knowledge_publication": False,
            "evidence_subgraph": False,
            **self._capabilities,
        }
        # This deployment intentionally has no final-answer route authorization;
        # callers cannot make the bootstrap claim otherwise.
        capabilities["answer_generation"] = False
        return {
            "schema_version": "local-playground-bootstrap-v1",
            "mode": (
                "retrieval-and-governance"
                if capabilities.get("ontology_governance")
                else "retrieval"
            ),
            "dataset": {
                "id": manifest["dataset_id"],
                "version": manifest["version"],
                "counts": dict(manifest["counts"]),
                "embedding": self._embedding_metadata
                or {
                    "provider": profile["provider"],
                    "model": profile["model"],
                    "dimensions": profile["dimensions"],
                    "warning": profile["warning"],
                },
            },
            "personas": [item.as_dict() for item in self.personas],
            "questions": list(self.questions),
            "defaults": {
                "question_id": "single_chunk-success-01",
                "retrieval_limits": asdict(PLAYGROUND_RETRIEVAL_LIMITS),
                "industrial_tbox_template": deepcopy(
                    DEFAULT_INDUSTRIAL_TBOX_TEMPLATE
                ),
            },
            "capabilities": capabilities,
        }

    def issue_session(self, persona_id: str, *, now: int | None = None) -> dict[str, Any]:
        if not isinstance(persona_id, str) or _PERSONA_ID.fullmatch(persona_id) is None:
            raise KeyError("unknown Playground persona")
        persona = self.personas_by_id.get(persona_id)
        if persona is None:
            raise KeyError("unknown Playground persona")
        issued_at = int(time.time()) if now is None else int(now)
        expires_at = issued_at + PLAYGROUND_TOKEN_LIFETIME_SECONDS
        token = jwt.encode(
            {
                "iss": PLAYGROUND_ISSUER,
                "aud": PLAYGROUND_AUDIENCE,
                "sub": persona.principal_id,
                "tenant_id": persona.tenant_id,
                "groups": list(persona.groups),
                "scope": " ".join(persona.scopes),
                "iat": issued_at,
                "exp": expires_at,
            },
            self._signing_key,
            algorithm="HS256",
            headers={"typ": "JWT"},
        )
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "identity": persona.as_dict(),
        }


class FixtureQueryEmbedder:
    """Use reviewed vectors, with an honest BM25-only fallback for free text."""

    def __init__(self, fixture: DevelopmentCorpus) -> None:
        profile = fixture.build.manifest["embedding_profile"]
        self.embedding_space_id = str(profile["embedding_space_id"])
        self.dimensions = int(profile["dimensions"])
        neutral_index = int(profile["feature_count"])
        if not 0 <= neutral_index < self.dimensions:
            raise ValueError("fixture has no unused dimension for BM25-only queries")

        self._vectors_by_query = {
            str(question["query"]): tuple(
                float(value) for value in fixture.query_vector(question)
            )
            for question in fixture.build.questions
        }
        chunk_vectors = (
            fixture.vectors_by_id[str(chunk["chunk_id"])]
            for chunk in fixture.build.chunks
        )
        if any(float(vector[neutral_index]) != 0.0 for vector in chunk_vectors):
            raise ValueError(
                "fixture BM25-only dimension is not orthogonal to every Chunk"
            )
        neutral = [0.0] * self.dimensions
        neutral[neutral_index] = 1.0
        self._neutral_vector = tuple(neutral)

    def is_reviewed(self, query_text: str) -> bool:
        return query_text.strip() in self._vectors_by_query

    def embed(self, query_text: str, *, tenant_id: str) -> QueryEmbedding:
        del tenant_id  # The same synthetic question vector is safe across test tenants.
        normalized = query_text.strip()
        vector = self._vectors_by_query.get(normalized, self._neutral_vector)
        return QueryEmbedding(
            vector=vector,
            embedding_space_id=self.embedding_space_id,
            usage=ProviderUsage(model_calls=0, estimated_cost_usd=0.0),
        )


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    persona_id: str = Field(min_length=10, max_length=10, pattern=r"^persona-[0-9]{2}$")


def attach_playground_routes(app: FastAPI, catalog: PlaygroundCatalog) -> None:
    """Attach the local UI without changing production API authentication."""

    if not isinstance(app, FastAPI):
        raise TypeError("app must be a FastAPI instance")
    if not isinstance(catalog, PlaygroundCatalog):
        raise TypeError("catalog must be a PlaygroundCatalog")
    page = (
        files("graphrag_prod.playground")
        .joinpath("static")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )
    security_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }

    @app.get("/", include_in_schema=False)
    async def playground_root() -> RedirectResponse:
        return RedirectResponse("/playground", status_code=307)

    @app.get("/playground", response_class=HTMLResponse, include_in_schema=False)
    async def playground_page() -> HTMLResponse:
        return HTMLResponse(page, headers=security_headers)

    @app.get("/playground/bootstrap", include_in_schema=False)
    async def playground_bootstrap(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return catalog.bootstrap()

    @app.post("/playground/session", include_in_schema=False)
    async def playground_session(
        request: SessionRequest,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return catalog.issue_session(request.persona_id)
        except KeyError:
            # The public response deliberately reveals no valid persona inventory.
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="session identity not found") from None
