#!/usr/bin/env python3
"""Preview or resumably load the industrial corpus into the existing local service."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv
import neo4j
from openai import OpenAI

from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.loading import Neo4jIndustrialLoader, prepare_industrial_load, read_normalized_artifact
from graphrag_prod.industrial.sources import load_source_catalog
from graphrag_prod.ingestion.pipeline import EmbeddingProfile


ROOT = Path(__file__).resolve().parents[1]


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"required environment setting is missing: {name}")
    return value


class ConfiguredIndustrialEmbedder:
    """Bounded provider adapter in the exact configured Playground vector space."""

    def __init__(self, client: OpenAI, profile: EmbeddingProfile) -> None:
        self.client = client
        self.profile = profile

    def __call__(self, *, chunk, profile: EmbeddingProfile, **kwargs) -> tuple[float, ...]:
        if profile != self.profile:
            raise ValueError("industrial embedding space differs from configured provider")
        response = self.client.embeddings.create(
            input=[chunk.text], model=profile.model, dimensions=profile.dimensions, encoding_format="float",
        )
        if len(response.data) != 1 or response.data[0].index != 0:
            raise ValueError("embedding provider returned an unexpected result count")
        vector = tuple(float(value) for value in response.data[0].embedding)
        if len(vector) != profile.dimensions:
            raise ValueError("embedding provider returned an unexpected dimension")
        return vector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", action="store_true", help="Perform resumable writes; default is a provider-free preview")
    parser.add_argument("--normalized-source", type=Path, action="append", default=[])
    parser.add_argument("--catalog", type=Path, default=ROOT / "datasets/industrial-v1/sources.json")
    parser.add_argument("--contract", type=Path, default=ROOT / "contracts/industrial_knowledge.v1.json")
    parser.add_argument("--original-cache", type=Path, help="Pinned originals named {source_id}.pdf; required for official artifacts")
    parser.add_argument("--corpus-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    corpus = build_corpus(args.corpus_root)
    catalog = load_source_catalog(args.catalog)
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    if args.normalized_source and args.original_cache is None:
        raise ValueError("--normalized-source requires --original-cache for independent derivation verification")
    artifacts = tuple(read_normalized_artifact(path, catalog, args.original_cache) for path in args.normalized_source)
    profile = EmbeddingProfile(
        "dashscope-openai-compatible", _required("EMBEDDING_MODEL") if args.load else os.getenv("EMBEDDING_MODEL", "text-embedding-v4"), "api-v1",
        int(_required("EMBEDDING_DIMENSIONS") if args.load else os.getenv("EMBEDDING_DIMENSIONS", "1024")), "provider-default",
    )
    if not args.load:
        result = {**prepare_industrial_load(corpus, profile, ingested_at=datetime.now(UTC),
            normalized_artifacts=artifacts, catalog=catalog, contract=contract, original_cache=args.original_cache).report(), "mode": "dry-run", "provider_calls": 0}
    else:
        if _required("PLAYGROUND_ALLOW_DISPOSABLE_DB") != "1":
            raise ValueError("industrial local writes require PLAYGROUND_ALLOW_DISPOSABLE_DB=1")
        uri = _required("PLAYGROUND_NEO4J_URI")
        parsed = urlsplit(uri)
        if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError("industrial development loader requires a loopback Neo4j URI")
        base_url = _required("OPENAI_BASE_URL")
        parsed_provider = urlsplit(base_url)
        if parsed_provider.scheme != "https" or parsed_provider.hostname not in {
            "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
        } or parsed_provider.username or parsed_provider.password:
            raise ValueError("configured provider must use an official DashScope HTTPS endpoint")
        with OpenAI(api_key=_required("OPENAI_API_KEY"), base_url=base_url, timeout=30.0, max_retries=1) as client:
            with neo4j.GraphDatabase.driver(uri, auth=(
                _required("PLAYGROUND_NEO4J_USER"), _required("PLAYGROUND_NEO4J_PASSWORD")),
                max_connection_pool_size=4, connection_acquisition_timeout=15.0,
            ) as driver:
                driver.verify_connectivity()
                result = Neo4jIndustrialLoader(driver, os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j")).load(
                    corpus, profile, ConfiguredIndustrialEmbedder(client, profile),
                    normalized_artifacts=artifacts, catalog=catalog, contract=contract, original_cache=args.original_cache,
                )
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Detailed database/provider exceptions can contain secrets or source
        # content. Durable loader checkpoints retain a safe failure category.
        raise SystemExit(f"Industrial loading failed ({type(error).__name__}); inspect bounded local validation and load status.") from None
