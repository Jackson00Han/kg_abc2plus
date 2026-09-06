#!/usr/bin/env python3
"""JSON-lines retrieval worker importing only the explicitly selected checkout.

No gold, industrial scope resolver, embedding provider, or evaluation module is
imported here. The caller supplies already prepared request fields and vectors.
The same process boundary also applies the deadline to current-engine captures.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--check-import", action="store_true", help="Check module provenance without opening a database driver")
    args = parser.parse_args()
    source = (args.source_root.resolve() / "src").resolve()
    if not source.is_dir():
        raise RuntimeError("worker source root is unavailable")
    # Do not rely on the editable install's .pth file to choose an implementation.
    sys.path.insert(0, str(source))
    import neo4j
    from graphrag_prod.domain.access import Principal
    from graphrag_prod.retrieval.engine import Neo4jRetrievalEngine
    from graphrag_prod.retrieval.models import RetrievalLimits, RetrievalRequest, VersionFilter

    for name, module in tuple(sys.modules.items()):
        if name == "graphrag_prod" or name.startswith("graphrag_prod."):
            file = getattr(module, "__file__", None)
            if file is not None and not Path(file).resolve().is_relative_to(source):
                raise RuntimeError("worker imported a different graphrag checkout")
    if any(name.startswith("graphrag_prod.industrial") for name in sys.modules):
        raise RuntimeError("retrieval worker must not import industrial gold or resolver")
    engine_checksum = hashlib.sha256((source / "graphrag_prod/retrieval/engine.py").read_bytes()).hexdigest()
    if args.check_import:
        print(json.dumps({"source_root": str(source), "implementation_checksum": engine_checksum,
                          "industrial_modules_imported": False}), flush=True)
        return
    logging.getLogger("neo4j").setLevel(logging.CRITICAL)
    with neo4j.GraphDatabase.driver(
        os.environ["PLAYGROUND_NEO4J_URI"],
        auth=(os.environ["PLAYGROUND_NEO4J_USER"], os.environ["PLAYGROUND_NEO4J_PASSWORD"]),
        max_connection_pool_size=2, connection_acquisition_timeout=10.0,
    ) as driver:
        engine = Neo4jRetrievalEngine(driver, os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j"), transaction_timeout_seconds=30.0)
        for _ in range(128):
            line = sys.stdin.readline(1024 * 1024 + 1)
            if not line:
                return
            if len(line.encode("utf-8")) > 1024 * 1024 or not line.endswith("\n"):
                raise RuntimeError("worker request exceeds input bound")
            raw = json.loads(line)
            started = time.monotonic()
            output = {"case_id": raw["case_id"], "variant": raw["variant"],
                "query_checksum": raw["query_checksum"], "query_vector_checksum": raw["query_vector_checksum"],
                "implementation_checksum": engine_checksum, "chunks": [], "trace": None, "error": None}
            try:
                filters = dict(raw["version_filter"])
                for key in ("document_ids", "version_ids"):
                    filters[key] = frozenset(filters.get(key, ()))
                if filters.get("published_at_or_before"):
                    filters["published_at_or_before"] = datetime.fromisoformat(filters["published_at_or_before"])
                request = RetrievalRequest(
                    query_text=raw["query"], query_vector=tuple(raw["vector"]),
                    principal=Principal(raw["principal"]["principal_id"], raw["principal"]["tenant_id"], frozenset(raw["principal"]["groups"])),
                    query_embedding_space_id=raw["embedding_space_id"],
                    limits=RetrievalLimits(**raw["limits"]), version_filter=VersionFilter(**filters),
                )
                result = engine.retrieve(request)
                output["chunks"] = [_plain(asdict(chunk)) for chunk in result.chunks]
                output["trace"] = result.trace.as_dict()
            except Exception as error:
                # Exception detail may contain protected source material or DSNs.
                output["error"] = type(error).__name__
            output["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"worker_error": type(error).__name__}), flush=True)
        raise SystemExit(2) from None
