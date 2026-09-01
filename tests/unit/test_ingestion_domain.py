"""Stable ingestion identities and immutable snapshot model tests."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import unittest

from graphrag_prod.domain import (
    GraphPipelineProfile,
    KnowledgeSnapshot,
    chunk_embedding_id,
    content_checksum,
    derivation_artifact_id,
    embedding_index_generation_id,
    ingestion_job_id,
    ingestion_task_id,
    knowledge_snapshot_id,
    pipeline_profile_id,
)
from tests.fixtures.domain import make_bundle


PROFILE_SIGNATURES = (
    "unicode-nfc:v1",
    "fixed-size:v1:size=500:overlap=100",
    "deterministic-extractor:v1",
    "entity-and-relation-prompt:sha256:abc123",
    "company-filings:v1",
    "git:0123456789abcdef",
)


def make_profile() -> GraphPipelineProfile:
    identifier = pipeline_profile_id(*PROFILE_SIGNATURES)
    return GraphPipelineProfile(identifier, *PROFILE_SIGNATURES)


def make_snapshot() -> KnowledgeSnapshot:
    bundle = make_bundle()
    profile = make_profile()
    identifier = knowledge_snapshot_id(bundle.version.version_id, profile.profile_id)
    return KnowledgeSnapshot(
        snapshot_id=identifier,
        tenant_id=bundle.document.tenant_id,
        document_id=bundle.document.document_id,
        version_id=bundle.version.version_id,
        profile_id=profile.profile_id,
        manifest_hash=content_checksum("chunk-manifest:v1"),
        expected_chunk_count=1,
        created_at=datetime(2024, 10, 1, 12, 0, tzinfo=UTC),
    )


class IngestionIdentityTests(unittest.TestCase):
    def test_profile_and_snapshot_ids_are_stable_and_profile_sensitive(self) -> None:
        profile = pipeline_profile_id(*PROFILE_SIGNATURES)
        self.assertEqual(profile, pipeline_profile_id(*PROFILE_SIGNATURES))
        changed = (*PROFILE_SIGNATURES[:-1], "git:different")
        self.assertNotEqual(profile, pipeline_profile_id(*changed))

        bundle = make_bundle()
        snapshot = knowledge_snapshot_id(bundle.version.version_id, profile)
        self.assertEqual(
            snapshot,
            knowledge_snapshot_id(bundle.version.version_id, profile),
        )
        self.assertNotEqual(
            snapshot,
            knowledge_snapshot_id(
                bundle.version.version_id,
                pipeline_profile_id(*changed),
            ),
        )

    def test_job_task_artifact_and_generation_ids_are_tenant_scoped(self) -> None:
        bundle = make_bundle()
        job = ingestion_job_id("tenant-a", "upsert", "request-123")
        self.assertEqual(job, ingestion_job_id("tenant-a", "upsert", "request-123"))
        self.assertNotEqual(job, ingestion_job_id("tenant-b", "upsert", "request-123"))
        self.assertNotEqual(job, ingestion_job_id("tenant-a", "delete", "request-123"))

        task = ingestion_task_id(job, bundle.chunk.chunk_id)
        self.assertEqual(task, ingestion_task_id(job, bundle.chunk.chunk_id))

        profile = pipeline_profile_id(*PROFILE_SIGNATURES)
        input_hash = content_checksum("source inputs")
        artifact = derivation_artifact_id("tenant-a", "entities", input_hash, profile)
        self.assertEqual(
            artifact,
            derivation_artifact_id("tenant-a", "entities", input_hash.upper(), profile),
        )
        self.assertNotEqual(
            artifact,
            derivation_artifact_id("tenant-a", "assertions", input_hash, profile),
        )

        generation = embedding_index_generation_id(
            "tenant-a", bundle.embedding.embedding_space_id, 1
        )
        self.assertEqual(
            generation,
            embedding_index_generation_id(
                "tenant-a", bundle.embedding.embedding_space_id, 1
            ),
        )
        self.assertNotEqual(
            generation,
            embedding_index_generation_id(
                "tenant-a", bundle.embedding.embedding_space_id, 2
            ),
        )

    def test_new_id_inputs_are_strictly_validated(self) -> None:
        bundle = make_bundle()
        profile = pipeline_profile_id(*PROFILE_SIGNATURES)
        invalid_calls = (
            lambda: pipeline_profile_id("", *PROFILE_SIGNATURES[1:]),
            lambda: knowledge_snapshot_id("", profile),
            lambda: ingestion_job_id("tenant-a", "", "request-123"),
            lambda: ingestion_task_id("job-id", ""),
            lambda: derivation_artifact_id(
                "tenant-a", "entities", "not-a-sha256", profile
            ),
            lambda: embedding_index_generation_id(
                "tenant-a", bundle.embedding.embedding_space_id, 0
            ),
            lambda: embedding_index_generation_id(
                "tenant-a", bundle.embedding.embedding_space_id, True
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


class IngestionModelTests(unittest.TestCase):
    def test_pipeline_profile_self_validates_its_id(self) -> None:
        profile = make_profile()
        self.assertEqual(profile.profile_id, pipeline_profile_id(*PROFILE_SIGNATURES))
        with self.assertRaisesRegex(ValueError, "profile_id"):
            dataclasses.replace(profile, splitter_signature="different:v1")

    def test_snapshot_self_validates_identity_manifest_and_count(self) -> None:
        snapshot = make_snapshot()
        self.assertEqual(
            snapshot.snapshot_id,
            knowledge_snapshot_id(snapshot.version_id, snapshot.profile_id),
        )
        invalid_changes = (
            {"profile_id": "different-profile"},
            {"manifest_hash": "not-a-sha256"},
            {"expected_chunk_count": 0},
            {"expected_chunk_count": True},
            {"created_at": datetime(2024, 10, 1, 12, 0)},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dataclasses.replace(snapshot, **changes)

    def test_embedding_vector_checksum_is_stable(self) -> None:
        embedding = dataclasses.replace(
            make_bundle().embedding,
            vector=(0.125, -0.5, 1, 2.0),
        )
        self.assertEqual(embedding.vector, (0.125, -0.5, 1.0, 2.0))
        self.assertEqual(
            embedding.vector_checksum,
            content_checksum("[0.125,-0.5,1.0,2.0]"),
        )
        self.assertEqual(
            embedding.vector_checksum,
            dataclasses.replace(embedding).vector_checksum,
        )
        self.assertIsNone(make_bundle().embedding.vector_checksum)

    def test_embedding_self_validates_its_chunk_scoped_identity(self) -> None:
        embedding = make_bundle().embedding

        self.assertEqual(
            embedding.embedding_id,
            chunk_embedding_id(
                embedding.chunk_id,
                embedding.embedding_space_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "embedding_id"):
            dataclasses.replace(embedding, embedding_id="forged-embedding-id")

    def test_embedding_vector_rejects_wrong_shape_and_nonfinite_values(self) -> None:
        embedding = make_bundle().embedding
        invalid_vectors = (
            (0.0,),
            (0.0, 1.0, 2.0, float("nan")),
            (0.0, 1.0, 2.0, float("inf")),
            (0.0, 1.0, 2.0, "not-a-number"),
        )
        for vector in invalid_vectors:
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                dataclasses.replace(embedding, vector=vector)

    def test_embedding_vector_requires_a_float32_cosine_indexable_norm(self) -> None:
        embedding = make_bundle().embedding
        invalid_vectors = (
            (0.0, 0.0, 0.0, 0.0),
            (1e-50, 0.0, 0.0, 0.0),
            (1e39, 0.0, 0.0, 0.0),
            (3e38, 3e38, 0.0, 0.0),
        )
        for vector in invalid_vectors:
            with (
                self.subTest(vector=vector),
                self.assertRaisesRegex(ValueError, "(?:float32|non-zero finite norm)"),
            ):
                dataclasses.replace(embedding, vector=vector)


if __name__ == "__main__":
    unittest.main()
