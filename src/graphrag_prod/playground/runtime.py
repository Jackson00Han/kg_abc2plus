"""Safe local-only adapters and routes for the retrieval Playground.

The Playground deliberately reuses the authenticated production API.  Routes
under ``/playground`` only supply public synthetic corpus metadata and short-
lived test identities; every retrieval still crosses ``/v1`` and therefore
the normal JWT, tenant, group, rate-limit, and bounded-runtime controls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.resources import files
import ipaddress
import re
import time
from typing import Any, Mapping, Protocol

from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse
import jwt
from pydantic import BaseModel, ConfigDict, Field

from graphrag_prod.retrieval import RetrievalLimits
from .industrial_demo import get_industrial_demo_kit
from .reset import PlaygroundResetController

PLAYGROUND_ISSUER = "sample-graphrag-local-playground"
PLAYGROUND_AUDIENCE = "sample-graphrag-local-api"
PLAYGROUND_TOKEN_LIFETIME_SECONDS = 900
PLAYGROUND_SCOPES = (
    "retrieval:read",
    "knowledge:graph:read",
    "ontology:read",
    "ontology:write",
    "ontology:publish",
    "knowledge:import",
    "knowledge:construct",
    "knowledge:review",
    "knowledge:publish",
    "knowledge:quality",
    "knowledge:lifecycle",
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

class DevelopmentCorpus(Protocol):
    """Metadata supplied by the bundled pump example."""

    build: Any


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
    if tenant_id in {"demo-a", "demo-b"}:
        labels = {"public": "公开阅读", "maintenance": "维护上传", "reviewer": "知识复核", "administrator": "知识管理员"}
        return f"{'一号泵站' if tenant_id == 'demo-a' else '二号泵站（独立租户）'} · " + " / ".join(labels[group] for group in groups)
    tenant = tenant_id.removeprefix("tenant-").replace("-", " ").title()
    names = [group.split("-", 1)[-1].replace("-", " ").title() for group in groups]
    return f"Tenant {tenant} · {' + '.join(names)}"


def _persona_scopes(tenant_id: str, groups: tuple[str, ...]) -> tuple[str, ...]:
    """Assign local demo duties without weakening production-style RBAC."""

    selected = {"retrieval:read", "ontology:read", "knowledge:graph:read"}
    group_set = frozenset(groups)
    if tenant_id == "demo-a":
        if "administrator" in group_set:
            selected.update(PLAYGROUND_SCOPES)
        elif "maintenance" in group_set:
            selected.add("knowledge:construct")
        elif "reviewer" in group_set:
            selected.add("knowledge:review")
        return tuple(scope for scope in PLAYGROUND_SCOPES if scope in selected)
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
        self.workspaces = None
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
            "source_only_upload": False,
            "ontology_governance": False,
            "human_review": False,
            "knowledge_publication": False,
            "published_graph_quality": False,
            "evidence_subgraph": False,
            **self._capabilities,
        }
        # This deployment intentionally has no final-answer route authorization;
        # callers cannot make the bootstrap claim otherwise.
        capabilities["answer_generation"] = False
        return {
            "industrial": {"enabled": False},
            "enabled_tenants": sorted({item.tenant_id for item in self.personas}),
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
                "industrial_demo": get_industrial_demo_kit(),
            },
            "capabilities": capabilities,
        }

    def issue_session(self, persona_id: str, *, now: int | None = None) -> dict[str, Any]:
        if self.workspaces is not None:
            return self.workspaces.issue_session(persona_id)
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


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    persona_id: str = Field(min_length=10, max_length=96,
        pattern=r"^(persona-[0-9]{2}|workspace-[0-9a-f-]{36}-[1-9][0-9]*-(admin|user))$")


def attach_playground_routes(
    app: FastAPI, catalog: PlaygroundCatalog,
    *, reset_controller: PlaygroundResetController | None = None,
) -> None:
    """Attach the local UI without changing production API authentication."""

    if not isinstance(app, FastAPI):
        raise TypeError("app must be a FastAPI instance")
    if not isinstance(catalog, PlaygroundCatalog):
        raise TypeError("catalog must be a PlaygroundCatalog")
    if reset_controller is not None:
        if not isinstance(reset_controller, PlaygroundResetController):
            raise TypeError("reset_controller must be PlaygroundResetController")
        reset_controller.install_middleware(app)
        reset_controller.attach_routes(app)
    from .industrial_web import HEADERS, KNOWLEDGE_ASSETS, attach_industrial_web

    security_headers = HEADERS

    @app.get("/", include_in_schema=False)
    async def playground_root() -> RedirectResponse:
        return RedirectResponse("/industrial", status_code=307, headers=security_headers)

    @app.get("/playground", include_in_schema=False)
    async def playground_page() -> RedirectResponse:
        return RedirectResponse("/industrial", status_code=307, headers=security_headers)

    @app.get("/playground/assets/{asset_name}", include_in_schema=False)
    async def knowledge_asset(asset_name: str) -> Response:
        from fastapi import HTTPException
        if asset_name not in KNOWLEDGE_ASSETS:
            raise HTTPException(status_code=404, detail="asset not found")
        asset = files("graphrag_prod.playground").joinpath("static", "knowledge", asset_name)
        if not asset.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return Response(asset.read_bytes(), media_type="text/css" if asset_name.endswith(".css") else "text/javascript", headers=security_headers)

    @app.get("/playground/bootstrap", include_in_schema=False)
    async def playground_bootstrap(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        payload = catalog.bootstrap()
        if catalog.workspaces is not None:
            payload.update(catalog.workspaces.bootstrap())
            payload["capabilities"]["knowledge_bases"] = True
            payload["capabilities"]["knowledge_base_reset"] = True
        if getattr(catalog, "pump_only", False):
            payload["data_scope"] = "pump-only"
            payload["industrial"] = {"enabled": False}
            payload["questions"] = []
            payload["enabled_tenants"] = sorted({p["tenant_id"] for p in payload["personas"]})
            payload["dataset"] = {"id": "industrial-demo-v1", "version": "1", "embedding": catalog._embedding_metadata}
            payload["defaults"]["question_id"] = None
        if reset_controller is not None:
            payload["local_reset"] = reset_controller.bootstrap()
        return payload

    @app.get("/playground/demo-files/{filename}", include_in_schema=False)
    async def playground_demo_file(filename: str) -> Response:
        # This allowlist serves committed synthetic teaching material only.
        # Runtime documents and filesystem paths never enter this route.
        if filename == "authoritative_instances.template.json":
            resource = files("graphrag_prod.playground").joinpath("static", "industrial-demo-v1", filename)
            return Response(resource.read_bytes(), media_type="application/json", headers={
                **security_headers, "Content-Disposition": f'attachment; filename="{filename}"',
            })
        item = next(
            (entry for entry in get_industrial_demo_kit()["files"]
             if entry["filename"] == filename),
            None,
        )
        if item is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="demo file not found")
        return Response(
            item["text"].encode("utf-8"),
            media_type=item["mime_type"],
            headers={
                **security_headers,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

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

    attach_industrial_web(app)
