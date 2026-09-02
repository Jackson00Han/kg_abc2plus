"""Determinism, topology, provenance, and materialization tests for load-v1."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from graphrag_prod.domain import (
    Principal,
    chunk_embedding_id,
    chunk_id,
    content_checksum,
    document_id,
    embedding_index_generation_id,
    entity_id,
    knowledge_snapshot_id,
    mention_id,
    version_id,
)
from scripts.build_load_corpus import (
    ACTIVE_VERSION_NUMBER,
    CHUNKS_PER_VERSION,
    DEFAULT_DATASET_DIR,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_SPACE_ID,
    EXTRACTOR_SIGNATURE,
    PIPELINE_PROFILE_ID,
    PRIMARY_TENANT_ID,
    SCENARIOS,
    SCHEMA_SIGNATURE,
    SPLITTER_SIGNATURE,
    VERSIONS_PER_DOCUMENT,
    build_manifest,
    build_retrieval_workload,
    canonical_json_bytes,
    check_compact_dataset,
    check_materialized_dataset,
    deterministic_vector,
    graph_records_from_bundle,
    iter_chunks,
    iter_documents,
    iter_entities,
    iter_graph_record_bundles,
    iter_mentions,
    iter_version_bundles,
    materialize_dataset,
)
from scripts.load_production_corpus import _canonical_node_payload, _identity
from scripts.run_large_database_quality import (
    _load_quality_gold,
    _source_authorized_chunk_ids,
)
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


ROOT = Path(__file__).parents[2]


class LoadCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = build_manifest()

    def test_checked_in_compact_dataset_matches_streamed_builder(self) -> None:
        self.assertEqual(
            check_compact_dataset(DEFAULT_DATASET_DIR, self.manifest),
            (),
        )
        self.assertEqual(
            set(path.name for path in DEFAULT_DATASET_DIR.iterdir()),
            {"NOTICE.txt", "manifest.json"},
        )

    def test_canonical_state_uses_exact_business_ids_and_verified_vectors(self) -> None:
        self.assertEqual(
            _identity(["Entity"], {"tenant_id": "tenant", "entity_id": "entity-1"}),
            "Entity:entity_id:entity-1",
        )
        self.assertEqual(
            _identity(
                ["EntityMention"],
                {"tenant_id": "tenant", "mention_id": "mention-1"},
            ),
            "EntityMention:mention_id:mention-1",
        )
        vector = (0.8, 0.6)
        serialized = json.dumps(
            vector,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        payload = _canonical_node_payload(
            ["ChunkEmbedding"],
            {
                "embedding_id": "embedding-1",
                "vector": list(vector),
                "vector_checksum": checksum,
            },
        )
        self.assertEqual(payload["properties"]["verified_vector_sha256"], checksum)
        self.assertNotIn("vector", payload["properties"])
        with self.assertRaisesRegex(ValueError, "vector_checksum"):
            _canonical_node_payload(
                ["ChunkEmbedding"],
                {
                    "embedding_id": "embedding-1",
                    "vector": list(vector),
                    "vector_checksum": "0" * 64,
                },
            )

        first = _canonical_node_payload(
            ["IngestionJob"],
            {
                "job_id": "job-1",
                "status": "SUCCEEDED",
                "attempts": 1,
                "updated_at": "first",
            },
        )
        replay = _canonical_node_payload(
            ["IngestionJob"],
            {
                "job_id": "job-1",
                "status": "SUCCEEDED",
                "attempts": 2,
                "updated_at": "second",
            },
        )
        self.assertEqual(first, replay)

    def test_quality_acl_oracle_is_derived_from_committed_source_records(self) -> None:
        quality_fixture = load_dev_corpus_fixture()
        quality_gold = _load_quality_gold(quality_fixture)
        questions_by_id = {
            question["id"]: question for question in quality_gold.questions
        }
        self.assertEqual(len(quality_gold.questions), 49)
        self.assertEqual(
            questions_by_id["cross_chunk-success-03"][
                "required_evidence_groups"
            ],
            [
                [
                    "b3fbc327-dfd1-537b-b4c4-f8be67bd004f",
                    "4ba4952d-485b-5035-83ac-f159c9ac3869",
                ],
                [
                    "3dfb6510-01be-5d47-9dd3-8f487140d52b",
                    "1f18e2ee-2ac8-5d6f-9b0b-c7cac2753e4b",
                ],
            ],
        )

        documents = {
            "doc-allowed": {
                "access_groups": ["finance"],
                "access_policy_id": "policy-1",
                "access_policy_version": 1,
                "document_id": "doc-allowed",
                "tenant_id": "tenant-a",
            },
            "doc-denied": {
                "access_groups": ["legal"],
                "access_policy_id": "policy-2",
                "access_policy_version": 1,
                "document_id": "doc-denied",
                "tenant_id": "tenant-a",
            },
        }
        chunks = [
            {
                "access_groups": ["finance"],
                "access_policy_id": "policy-1",
                "access_policy_version": 1,
                "chunk_id": "chunk-allowed",
                "document_id": "doc-allowed",
                "tenant_id": "tenant-a",
            },
            {
                "access_groups": ["legal"],
                "access_policy_id": "policy-2",
                "access_policy_version": 1,
                "chunk_id": "chunk-hidden",
                "document_id": "doc-denied",
                "tenant_id": "tenant-a",
            },
        ]
        fixture = SimpleNamespace(
            build=SimpleNamespace(chunks=chunks),
            documents_by_id=documents,
        )
        principal = Principal(
            "reader",
            "tenant-a",
            frozenset({"finance"}),
        )
        self.assertEqual(
            _source_authorized_chunk_ids(fixture, principal),  # type: ignore[arg-type]
            {"chunk-allowed"},
        )

    def test_production_reference_scale_and_topology_are_explicit(self) -> None:
        contract = json.loads(
            (ROOT / "contracts" / "acceptance.v1.json").read_text(
                encoding="utf-8"
            )
        )
        required_load_items = next(
            item["minimum_items"]
            for item in contract["datasets"]
            if item["id"] == "load-v1"
        )
        counts = self.manifest["counts"]
        self.assertGreaterEqual(
            counts["active_chunks"],
            contract["scope"]["minimum_validation_chunks"],
        )
        self.assertGreaterEqual(counts["load_items"], required_load_items)
        self.assertEqual(counts["active_chunks"], 12_000)
        self.assertEqual(counts["historical_chunks"], 12_000)
        self.assertEqual(counts["total_chunks"], 24_000)
        self.assertEqual(counts["documents"], 240)
        self.assertEqual(counts["entities"], 240)
        self.assertEqual(counts["mentions"], 24_000)
        self.assertEqual(counts["assertions"], 0)
        self.assertEqual(counts["versions"], 480)
        self.assertEqual(counts["tenants"], 5)
        self.assertEqual(counts["access_groups"], 25)
        self.assertEqual(
            set(self.manifest["coverage"]["lifecycle_scenarios"]),
            set(SCENARIOS),
        )
        self.assertTrue(self.manifest["synthetic"])
        self.assertEqual(
            self.manifest["embedding_profile"]["quality_claim"], "none"
        )
        governance = json.loads(
            (ROOT / "contracts" / "graph_governance.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            SCHEMA_SIGNATURE,
            {policy["policy_id"] for policy in governance["policies"]},
        )
        primary = self.manifest["coverage"]["primary_tenant"]
        self.assertEqual(primary["tenant_id"], PRIMARY_TENANT_ID)
        self.assertEqual(primary["active_chunks"], 10_000)
        self.assertEqual(primary["public_active_chunks"], 5_000)
        self.assertEqual(primary["protected_active_chunks"], 5_000)
        self.assertEqual(len(self.manifest["coverage"]["canary_tenants"]), 4)
        self.assertEqual(
            self.manifest["coverage"]["primary_load_tenant"],
            PRIMARY_TENANT_ID,
        )
        self.assertEqual(
            self.manifest["coverage"]["load_principal_groups"],
            [
                f"{PRIMARY_TENANT_ID}-public",
                f"{PRIMARY_TENANT_ID}-group-01",
            ],
        )
        self.assertEqual(
            self.manifest["coverage"]["load_principal_acl"],
            {
                "access_groups": [
                    f"{PRIMARY_TENANT_ID}-group-01",
                    f"{PRIMARY_TENANT_ID}-public",
                ],
                "cross_tenant_active_chunks": 2_000,
                "cross_tenant_active_embeddings": 2_000,
                "denied_same_tenant_active_chunks": 2_500,
                "denied_same_tenant_active_embeddings": 2_500,
                "tenant_id": PRIMARY_TENANT_ID,
                "total_same_tenant_active_chunks": 10_000,
                "total_same_tenant_active_embeddings": 10_000,
                "visible_same_tenant_active_chunks": 7_500,
                "visible_same_tenant_active_embeddings": 7_500,
            },
        )
        self.assertEqual(
            {
                tenant_id: embedding_index_generation_id(
                    tenant_id,
                    EMBEDDING_SPACE_ID,
                    1,
                )
                for tenant_id in self.manifest["coverage"]["tenants"]
            },
            {
                "load-tenant-01": "bf694c4e-e7a9-5758-8418-56000e0b8774",
                "load-tenant-02": "b4df8766-d71b-5957-ab84-91ac606e9526",
                "load-tenant-03": "20396121-c96b-5866-8013-9b7ea45b8b12",
                "load-tenant-04": "6d5c490b-c56c-5cff-9aa9-a08908acb7cb",
                "load-tenant-05": "3ebac453-1366-55ee-9bab-437ed375007d",
            },
        )
        before_generation = self.manifest["graph_expectations"][
            "before_generation_activation"
        ]
        self.assertEqual(before_generation["business_node_count"], 73_927)
        self.assertEqual(
            before_generation["business_relationship_count"], 147_360
        )
        self.assertEqual(
            before_generation["label_counts"],
            {
                "Chunk": 24_000,
                "ChunkEmbedding": 24_000,
                "Document": 240,
                "DocumentVersion": 480,
                "Entity": 240,
                "EntityMention": 24_000,
                "GraphGovernancePolicy": 1,
                "GraphPipelineProfile": 1,
                "IngestionJob": 480,
                "InitialLoadJob": 480,
                "KnowledgeSnapshot": 480,
                "TenantCorpusState": 5,
            },
        )
        graph_shape = self.manifest["graph_expectations"][
            "after_generation_activation"
        ]
        self.assertEqual(graph_shape["business_node_count"], 73_932)
        self.assertEqual(graph_shape["business_relationship_count"], 147_365)
        self.assertEqual(
            graph_shape["label_counts"],
            {
                "Chunk": 24_000,
                "ChunkEmbedding": 24_000,
                "Document": 240,
                "DocumentVersion": 480,
                "EmbeddingGeneration_20396121c96b586680139b7e": 500,
                "EmbeddingGeneration_3ebac453136655ee9bab437e": 500,
                "EmbeddingGeneration_6d5c490bc56c5cff9aa9a089": 500,
                "EmbeddingGeneration_b4df8766d71b5957ab8491ac": 500,
                "EmbeddingGeneration_bf694c4ee7a9575884185600": 10_000,
                "EmbeddingIndexGeneration": 5,
                "Entity": 240,
                "EntityMention": 24_000,
                "GraphGovernancePolicy": 1,
                "GraphPipelineProfile": 1,
                "IngestionJob": 480,
                "InitialLoadJob": 480,
                "KnowledgeSnapshot": 480,
                "TenantCorpusState": 5,
            },
        )
        self.assertEqual(
            len(self.manifest["coverage"]["protected_same_tenant_chunk_ids"]),
            4,
        )
        self.assertEqual(
            len(self.manifest["coverage"]["cross_tenant_chunk_ids"]),
            4,
        )
        self.assertNotEqual(
            self.manifest["coverage"]["deletion_candidate"]["tenant_id"],
            PRIMARY_TENANT_ID,
        )

    def test_semantic_load_queries_are_versioned_and_replayable(self) -> None:
        workload = build_retrieval_workload()
        self.assertEqual(workload, self.manifest["retrieval_workload"])
        self.assertEqual(workload["dataset_id"], "load-v1")
        self.assertEqual(workload["schema_version"], "load-retrieval-workload-v1")
        self.assertEqual(
            workload["anchor_selection"],
            "public-one-per-document-unique-cosine-neighborhood-v1",
        )
        self.assertEqual(workload["minimum_anchor_cosine"], 0.75)
        self.assertEqual(workload["query_count"], 64)
        self.assertEqual(workload["principal"]["tenant_id"], PRIMARY_TENANT_ID)
        queries = workload["queries"]
        self.assertEqual(
            {item["case_id"] for item in queries},
            {f"load-anchor-{index:02d}" for index in range(64)},
        )
        chunks = {
            item["chunk_id"]: item
            for item in iter_chunks(tenant_id=PRIMARY_TENANT_ID, active_only=True)
        }
        principal_groups = set(workload["principal"]["groups"])
        visible_chunks = [
            item
            for item in chunks.values()
            if principal_groups.intersection(item["access_groups"])
        ]
        self.assertEqual(len(visible_chunks), 7_500)
        visible_vectors = {
            item["chunk_id"]: deterministic_vector(item["chunk_id"])
            for item in visible_chunks
        }
        query_texts = {item["query_text"] for item in queries}
        self.assertEqual(len(query_texts), len(queries))
        self.assertEqual(
            len(
                {
                    chunks[item["expected_chunk_ids"][0]]["document_id"]
                    for item in queries
                }
            ),
            64,
        )
        for query in queries:
            self.assertEqual(query["tenant_id"], PRIMARY_TENANT_ID)
            self.assertEqual(query["embedding_space_id"], EMBEDDING_SPACE_ID)
            self.assertEqual(len(query["expected_chunk_ids"]), 1)
            chunk = chunks[query["expected_chunk_ids"][0]]
            self.assertEqual(query["expected_version_id"], chunk["version_id"])
            self.assertEqual(query["query_text"], chunk["text"].rstrip("\n"))
            target_vector = deterministic_vector(chunk["chunk_id"])
            target_support = tuple(
                index for index, value in enumerate(target_vector) if value
            )
            within_gate = [
                candidate["chunk_id"]
                for candidate in visible_chunks
                if sum(
                    target_vector[index]
                    * visible_vectors[candidate["chunk_id"]][index]
                    for index in target_support
                )
                >= 0.75
            ]
            self.assertEqual(within_gate, [chunk["chunk_id"]])
            self.assertEqual(
                query["query_vector_checksum"],
                hashlib.sha256(
                    json.dumps(
                        deterministic_vector(chunk["chunk_id"]),
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            )

    def test_every_version_and_chunk_has_stable_traceable_identity(self) -> None:
        versions_by_document: dict[str, list[dict]] = defaultdict(list)
        entities_by_document: dict[str, set[str]] = defaultdict(set)
        tenants: set[str] = set()
        groups: set[str] = set()
        scenarios: Counter[str] = Counter()
        primary_active_access_modes: Counter[str] = Counter()
        protected_canaries = set(
            self.manifest["coverage"]["protected_same_tenant_chunk_ids"]
        )
        cross_tenant_canaries = set(
            self.manifest["coverage"]["cross_tenant_chunk_ids"]
        )
        seen_protected_canaries: set[str] = set()
        seen_cross_tenant_canaries: set[str] = set()
        principal_groups = set(
            self.manifest["coverage"]["load_principal_groups"]
        )
        active_chunks = 0
        total_chunks = 0

        for bundle in iter_version_bundles():
            document = bundle.document
            versions_by_document[document["document_id"]].append(document)
            entities_by_document[document["document_id"]].add(
                bundle.entity["entity_id"]
            )
            tenants.add(document["tenant_id"])
            groups.update(document["access_groups"])
            if document["version_number"] == 1:
                scenarios[document["lifecycle_scenario"]] += 1
            self.assertEqual(
                document_id(document["tenant_id"], document["canonical_uri"]),
                document["document_id"],
            )
            self.assertEqual(
                content_checksum(document["normalized_text"]),
                document["version_checksum"],
            )
            self.assertEqual(
                version_id(
                    document["document_id"],
                    document["version_checksum"],
                    document["original_checksum"],
                ),
                document["version_id"],
            )
            self.assertEqual(len(bundle.chunks), CHUNKS_PER_VERSION)
            self.assertEqual(len(bundle.mentions), CHUNKS_PER_VERSION)
            expected_canonical_key = f"name:{document['document_key']}"
            self.assertEqual(
                bundle.entity["entity_id"],
                entity_id(
                    document["tenant_id"],
                    "Company",
                    expected_canonical_key,
                ),
            )
            self.assertEqual(
                bundle.entity["canonical_key"],
                expected_canonical_key,
            )
            cursor = 0
            reconstructed: list[str] = []
            for expected_ordinal, (chunk, mention) in enumerate(
                zip(bundle.chunks, bundle.mentions, strict=True)
            ):
                self.assertEqual(chunk["ordinal"], expected_ordinal)
                self.assertEqual(chunk["char_start"], cursor)
                self.assertEqual(
                    chunk["char_end"] - chunk["char_start"],
                    len(chunk["text"]),
                )
                self.assertEqual(
                    content_checksum(chunk["text"]),
                    chunk["checksum"],
                )
                self.assertEqual(
                    chunk_id(
                        document["version_id"],
                        SPLITTER_SIGNATURE,
                        chunk["ordinal"],
                        chunk["char_start"],
                        chunk["char_end"],
                        chunk["checksum"],
                    ),
                    chunk["chunk_id"],
                )
                vector = tuple(chunk["vector"])
                self.assertEqual(len(vector), EMBEDDING_DIMENSIONS)
                self.assertAlmostEqual(math.hypot(*vector), 1.0)
                self.assertEqual(vector, deterministic_vector(chunk["chunk_id"]))
                self.assertEqual(
                    chunk_embedding_id(chunk["chunk_id"], EMBEDDING_SPACE_ID),
                    chunk["embedding_id"],
                )
                self.assertEqual(mention["chunk_id"], chunk["chunk_id"])
                self.assertEqual(
                    mention["entity_id"],
                    bundle.entity["entity_id"],
                )
                self.assertEqual(
                    document["normalized_text"][
                        mention["char_start"] : mention["char_end"]
                    ],
                    mention["surface"],
                )
                self.assertEqual(
                    chunk["text"][
                        mention["relative_char_start"] : mention[
                            "relative_char_end"
                        ]
                    ],
                    mention["surface"],
                )
                self.assertEqual(
                    mention["mention_id"],
                    mention_id(
                        chunk["chunk_id"],
                        "Company",
                        mention["char_start"],
                        mention["char_end"],
                        mention["surface"],
                        EXTRACTOR_SIGNATURE,
                    ),
                )
                self.assertEqual(chunk["access_groups"], document["access_groups"])
                self.assertEqual(chunk["access_mode"], document["access_mode"])
                self.assertEqual(chunk["active"], document["active"])
                if chunk["tenant_id"] == PRIMARY_TENANT_ID and chunk["active"]:
                    primary_active_access_modes[chunk["access_mode"]] += 1
                if chunk["chunk_id"] in protected_canaries:
                    self.assertTrue(chunk["active"])
                    self.assertEqual(chunk["tenant_id"], PRIMARY_TENANT_ID)
                    self.assertEqual(chunk["access_mode"], "protected")
                    self.assertTrue(
                        principal_groups.isdisjoint(chunk["access_groups"])
                    )
                    seen_protected_canaries.add(chunk["chunk_id"])
                if chunk["chunk_id"] in cross_tenant_canaries:
                    self.assertTrue(chunk["active"])
                    self.assertNotEqual(chunk["tenant_id"], PRIMARY_TENANT_ID)
                    seen_cross_tenant_canaries.add(chunk["chunk_id"])
                active_chunks += int(chunk["active"])
                total_chunks += 1
                reconstructed.append(chunk["text"])
                cursor = chunk["char_end"]
            self.assertEqual("".join(reconstructed), document["normalized_text"])
            self.assertEqual(cursor, len(document["normalized_text"]))

        self.assertEqual(
            len(versions_by_document),
            self.manifest["counts"]["documents"],
        )
        for versions in versions_by_document.values():
            self.assertEqual(len(versions), VERSIONS_PER_DOCUMENT)
            self.assertEqual(
                [item["version_number"] for item in versions], [1, 2]
            )
            self.assertEqual(sum(item["active"] for item in versions), 1)
            self.assertTrue(
                next(item for item in versions if item["active"])["version_number"]
                == ACTIVE_VERSION_NUMBER
            )
            self.assertEqual(len({item["version_id"] for item in versions}), 2)
        self.assertTrue(
            all(len(entity_ids) == 1 for entity_ids in entities_by_document.values())
        )
        self.assertEqual(tenants, set(self.manifest["coverage"]["tenants"]))
        self.assertEqual(groups, set(self.manifest["coverage"]["access_groups"]))
        self.assertEqual(
            dict(scenarios),
            self.manifest["coverage"]["lifecycle_scenarios"],
        )
        self.assertEqual(active_chunks, self.manifest["counts"]["active_chunks"])
        self.assertEqual(total_chunks, self.manifest["counts"]["total_chunks"])
        self.assertEqual(
            primary_active_access_modes,
            {"public": 5_000, "protected": 5_000},
        )
        self.assertEqual(seen_protected_canaries, protected_canaries)
        self.assertEqual(seen_cross_tenant_canaries, cross_tenant_canaries)

    def test_streaming_graph_record_api_is_complete_and_filterable(self) -> None:
        first_source_bundle = next(iter_version_bundles())
        first = graph_records_from_bundle(first_source_bundle)
        self.assertEqual(
            first.document["document_id"],
            first.version["document_id"],
        )
        self.assertEqual(first.version["version_id"], first.snapshot["version_id"])
        self.assertEqual(
            first.snapshot["snapshot_id"],
            knowledge_snapshot_id(first.version["version_id"], PIPELINE_PROFILE_ID),
        )
        self.assertEqual(
            first.snapshot["expected_chunk_count"],
            CHUNKS_PER_VERSION,
        )
        self.assertEqual(len(first.snapshot["manifest_hash"]), 64)
        self.assertEqual(len(first.chunks), len(first.embeddings))
        self.assertEqual(len(first.entities), 1)
        self.assertEqual(len(first.mentions), CHUNKS_PER_VERSION)
        self.assertEqual(first.assertions, ())
        for chunk, embedding in zip(first.chunks, first.embeddings, strict=True):
            self.assertEqual(chunk["chunk_id"], embedding["chunk_id"])
            self.assertEqual(chunk["snapshot_id"], first.snapshot["snapshot_id"])
            self.assertEqual(embedding["snapshot_id"], first.snapshot["snapshot_id"])
            self.assertEqual(
                embedding["embedding_id"],
                chunk_embedding_id(chunk["chunk_id"], EMBEDDING_SPACE_ID),
            )

        first_direct = next(iter_graph_record_bundles())
        self.assertEqual(first_direct, first)
        self.assertEqual(sum(1 for _ in iter_documents()), 240)
        self.assertEqual(sum(1 for _ in iter_entities()), 240)
        self.assertEqual(
            sum(1 for _ in iter_mentions(active_only=True)),
            12_000,
        )
        primary_active = iter_chunks(
            tenant_id=PRIMARY_TENANT_ID,
            active_only=True,
        )
        self.assertEqual(sum(1 for _ in primary_active), 10_000)
        with self.assertRaisesRegex(ValueError, "unknown load-v1 tenant_id"):
            next(iter_chunks(tenant_id="not-a-load-tenant"))

    def test_manifest_and_stream_digests_replay_exactly(self) -> None:
        replay = build_manifest()
        self.assertEqual(replay, self.manifest)
        self.assertEqual(
            canonical_json_bytes(replay),
            (DEFAULT_DATASET_DIR / "manifest.json").read_bytes(),
        )
        self.assertEqual(len(replay["content_sha256"]), 64)
        for stream in replay["streams"].values():
            self.assertGreater(stream["records"], 0)
            self.assertGreater(stream["size_bytes"], 0)
            self.assertEqual(len(stream["sha256"]), 64)
        self.assertEqual(replay["streams"]["entities"]["records"], 240)
        self.assertEqual(replay["streams"]["mentions"]["records"], 24_000)

    def test_full_materialization_is_reproducible_and_tamper_evident(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            materialize_dataset(ROOT / "load-v1-expanded-must-not-exist")
        with TemporaryDirectory() as directory:
            target = Path(directory) / "load-v1-expanded"
            materialized = materialize_dataset(target)
            self.assertEqual(materialized, self.manifest)
            self.assertEqual(check_materialized_dataset(target), ())
            self.assertEqual(
                set(path.name for path in target.iterdir()),
                {
                    "NOTICE.txt",
                    "chunks.jsonl",
                    "documents.jsonl",
                    "entities.jsonl",
                    "manifest.json",
                    "mentions.jsonl",
                },
            )
            chunk_path = target / "chunks.jsonl"
            chunk_path.write_bytes(chunk_path.read_bytes() + b"\n")
            self.assertIn(
                "materialized load stream drifted: chunks.jsonl",
                check_materialized_dataset(target),
            )


if __name__ == "__main__":
    unittest.main()
