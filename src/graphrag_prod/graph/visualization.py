"""Versioned, private graph layout artifacts built from authorized publications.

Coordinates are presentation data, never a source of facts or permission. The
browser still validates its complete authorized evidence view on every read.
Only then may a matching artifact be loaded and clipped to the returned page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.publication_guard import MAX_PUBLICATION_MANIFEST_RECORDS

from .browse_models import GraphBrowseLimitExceeded, GraphBrowseUnavailable
from .view_tokens import canonical_json, digest


SCHEMA_VERSION = "graph-visualization.v1"
LAYOUT_VERSION = "cytoscape-3.34.2-cose-type-grid.v1"
MAX_ARTIFACT_BYTES = 16_000_000
_GENERATION_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


def _topology(view: Any) -> dict[str, Any]:
    nodes = [{"id": "n:" + key, "type": value["entity_type"]} for key, value in sorted(view.nodes.items())]
    edges = sorted({(value.subject_entity_id, value.object_entity_id)
                    for value in view.assertions.values() if value.object_entity_id is not None})
    types = sorted(item["name"] for item in view.schema.get("entity_types", ()))
    type_set = set(types)
    schema_edges = sorted({(source, target)
        for relation in view.schema.get("relationship_types", ())
        for source in relation.get("source_types", ()) for target in relation.get("target_types", ())
        if source in type_set and target in type_set})
    if len(nodes) > MAX_PUBLICATION_MANIFEST_RECORDS or len(edges) > MAX_PUBLICATION_MANIFEST_RECORDS:
        raise GraphBrowseLimitExceeded()
    return {"graph": {"nodes": nodes, "edges": [{"source": "n:" + a, "target": "n:" + b} for a, b in edges]},
            "ontology": {"nodes": [{"id": "t:" + key, "type": "ontology"} for key in types],
                         "edges": [{"source": "t:" + a, "target": "t:" + b} for a, b in schema_edges]}}


def _valid_layouts(value: Any, ids: set[str]) -> bool:
    if not isinstance(value, dict) or set(value) != {"network", "grouped"}:
        return False
    for layout in value.values():
        if not isinstance(layout, dict) or set(layout) != {"positions", "groups", "engine"}:
            return False
        if not isinstance(layout["engine"], str) or len(layout["engine"]) > 100:
            return False
        positions = layout["positions"]
        if not isinstance(positions, dict) or set(positions) != ids:
            return False
        for point in positions.values():
            if not isinstance(point, dict) or set(point) != {"x", "y"}:
                return False
            if any(type(number) not in {int, float} or not math.isfinite(number) or abs(number) > 10_000_000 for number in point.values()):
                return False
        if not isinstance(layout["groups"], list) or len(layout["groups"]) > 64:
            return False
        for group in layout["groups"]:
            if not isinstance(group, dict) or set(group) != {"type", "x", "y", "width", "height", "count"}:
                return False
            if not isinstance(group["type"], str) or not 1 <= len(group["type"]) <= 256:
                return False
            if type(group["count"]) is not int or not 1 <= group["count"] <= len(ids):
                return False
            if any(type(group[name]) not in {int, float} or not math.isfinite(group[name]) or not 0 <= group[name] <= 10_000_000
                   for name in ("x", "y", "width", "height")):
                return False
    return True


class GraphVisualizationStore:
    """Content-addressed files; fixed desktop layouts survive refresh/restart."""

    def __init__(self, directory: str | Path | None = None, *, timeout_seconds: float = 20.0) -> None:
        self.directory = Path(directory or os.environ.get("GRAPH_VISUALIZATION_DIR", ".local/graph-visualizations")).resolve()
        if not 0 < timeout_seconds <= 30:
            raise ValueError("visualization timeout is outside its bound")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _valid(artifact: Any, artifact_id: str, topology: dict[str, Any]) -> bool:
        if not isinstance(artifact, dict) or set(artifact) != {"schema_version", "layout_version", "artifact_id", "generated_at", "default_layout", "layouts", "ontology", "status", "content_checksum"}:
            return False
        content = {key: value for key, value in artifact.items() if key != "content_checksum"}
        try:
            datetime.fromisoformat(artifact["generated_at"])
            return (artifact["schema_version"] == SCHEMA_VERSION and artifact["layout_version"] == LAYOUT_VERSION
                and artifact["artifact_id"] == artifact_id and artifact["status"] == "READY"
                and artifact["default_layout"] in {"network", "grouped"}
                and digest(content) == artifact["content_checksum"]
                and _valid_layouts(artifact["layouts"], {node["id"] for node in topology["graph"]["nodes"]})
                and _valid_layouts(artifact["ontology"], {node["id"] for node in topology["ontology"]["nodes"]}))
        except (TypeError, ValueError, KeyError):
            return False

    def _read(self, path: Path, artifact_id: str, topology: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                return None
            artifact = json.loads(path.read_text(encoding="utf-8"))
            return artifact if self._valid(artifact, artifact_id, topology) else None
        except (OSError, ValueError):
            return None

    def prepare(self, principal: Principal, view: Any, *, trust_policy: str, version_filter: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(principal, Principal) or "knowledge:graph:read" not in principal.capabilities:
            raise PermissionError("graph read capability required")
        topology = _topology(view)
        # Layouts are scoped by tenant, source permissions, publication and the
        # exact authorized view. No file can carry nodes from another scope.
        artifact_id = digest({"layout_version": LAYOUT_VERSION, "tenant": principal.tenant_id,
            "groups": sorted(principal.groups), "publication": view.pin.publication_id,
            "ontology": view.pin.tbox_checksum, "trust_policy": trust_policy,
            "version_filter": version_filter, "view_digest": view.view_digest, "topology": topology})
        path = self.directory / (artifact_id + ".json")
        artifact = self._read(path, artifact_id, topology)
        if artifact is not None:
            return artifact
        if not _GENERATION_LOCK.acquire(timeout=self.timeout_seconds):
            raise GraphBrowseUnavailable()
        try:
            artifact = self._read(path, artifact_id, topology)
            if artifact is not None:
                return artifact
            node = shutil.which("node")
            if node is None:
                raise GraphBrowseUnavailable()
            source = canonical_json(topology)
            if len(source.encode()) > 8_000_000:
                raise GraphBrowseLimitExceeded()
            process = subprocess.run([node, "--max-old-space-size=192", str(Path(__file__).with_name("layout.cjs"))],
                input=source, text=True, capture_output=True, timeout=self.timeout_seconds,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}, check=False)
            if process.returncode or len(process.stdout) > MAX_ARTIFACT_BYTES:
                raise GraphBrowseUnavailable()
            layouts = json.loads(process.stdout)
            artifact = {"schema_version": SCHEMA_VERSION, "layout_version": LAYOUT_VERSION, "artifact_id": artifact_id,
                "generated_at": datetime.now(UTC).isoformat(), "status": "READY",
                "default_layout": "network" if topology["graph"]["edges"] else "grouped", **layouts}
            artifact["content_checksum"] = digest(artifact)
            if not self._valid(artifact, artifact_id, topology):
                raise GraphBrowseUnavailable()
            encoded = canonical_json(artifact)
            if len(encoded.encode()) > MAX_ARTIFACT_BYTES:
                raise GraphBrowseLimitExceeded()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return artifact
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise GraphBrowseUnavailable() from error
        finally:
            _GENERATION_LOCK.release()

    @staticmethod
    def page(artifact: dict[str, Any], nodes: dict[str, Any], selected: set[str]) -> dict[str, Any]:
        """Project cached coordinates to this page without exposing hidden IDs."""
        ids = {"n:" + key for key in selected}
        counts: dict[str, int] = {}
        for key in selected:
            kind = nodes[key]["entity_type"]
            counts[kind] = counts.get(kind, 0) + 1
        layouts = {name: {**layout,
            "positions": {key: point for key, point in layout["positions"].items() if key in ids},
            "groups": [{**group, "count": counts[group["type"]]} for group in layout["groups"] if group["type"] in counts]}
            for name, layout in artifact["layouts"].items()}
        return {key: value for key, value in artifact.items() if key != "content_checksum"} | {"layouts": layouts}


def prepare_publication_visualizations(browser: Any, principal: Principal, *, publication_id: str | None = None) -> bool:
    """Post-commit/read-only warmup; failure must not misreport a committed publish."""
    try:
        browser.prepare_visualizations(principal, publication_id=publication_id)
        return True
    except Exception:
        # No principal IDs, source text, provider secrets or exception bodies.
        _LOG.warning("Publication visualization preparation failed; a later authorized read can retry")
        return False


class GraphVisualizationPreparationQueue:
    """A bounded post-commit worker keeps publication responses unambiguous."""

    def __init__(self, browser: Any) -> None:
        self.browser = browser
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-visualization")
        self.slots = threading.BoundedSemaphore(4)

    def submit(self, principals: tuple[Principal, ...], publication_id: str) -> bool:
        if not 1 <= len(principals) <= 2:
            raise ValueError("visualization preparation requires one or two authorized readers")
        if not self.slots.acquire(blocking=False):
            _LOG.warning("Visualization preparation queue is full; an authorized read can retry")
            return False

        def run() -> None:
            try:
                for principal in principals:
                    prepare_publication_visualizations(self.browser, principal, publication_id=publication_id)
            finally:
                self.slots.release()

        try:
            self.executor.submit(run)
        except RuntimeError:
            self.slots.release()
            return False
        return True

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
