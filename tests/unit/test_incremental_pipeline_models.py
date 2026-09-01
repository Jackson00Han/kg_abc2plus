"""Pure contracts for durable provider-oriented ingestion requests."""

from __future__ import annotations

import dataclasses
from datetime import datetime
import unittest

from graphrag_prod.domain import embedding_space_id
from graphrag_prod.ingestion import (
    ChunkSeed,
    EmbeddingProfile,
    IncrementalIngestionRequest,
)
from tests.fixtures.ingestion import (
    CHUNKS_V1,
    FIXED_TIME,
    make_governance_policy,
    make_profile,
)


def _seeds() -> tuple[ChunkSeed, ...]:
    result: list[ChunkSeed] = []
    char_start = 0
    for ordinal, spec in enumerate(CHUNKS_V1):
        char_end = char_start + len(spec.text)
        result.append(
            ChunkSeed(
                ordinal=ordinal,
                text=spec.text,
                char_start=char_start,
                char_end=char_end,
                page_number=1,
                section=f"Metric {ordinal + 1}",
            )
        )
        char_start = char_end
    return tuple(result)


def _embedding_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="fixture",
        model="durable-provider-four-dimensional",
        revision="v1",
        dimensions=4,
        normalization="none",
    )


def _request() -> IncrementalIngestionRequest:
    return IncrementalIngestionRequest(
        operation_key="pipeline-unit-contract",
        tenant_id="tenant-pipeline-unit",
        canonical_uri="https://example.com/pipeline-unit",
        title="Pipeline unit contract",
        source_name="unit-fixture",
        version_number=1,
        mime_type="text/plain",
        language="en",
        published_at=FIXED_TIME,
        ingested_at=FIXED_TIME,
        original_checksum=None,
        access_policy_id="tenant-pipeline-unit:readers",
        access_policy_version=1,
        access_groups=frozenset({"readers"}),
        source_generation=0,
        expected_active_snapshot_id=None,
        chunks=_seeds(),
        profile=make_profile(),
        governance_policy=make_governance_policy(),
        embedding_profile=_embedding_profile(),
        max_attempts=3,
    )


class IncrementalPipelineModelTests(unittest.TestCase):
    def test_embedding_profile_has_one_stable_vector_space_identity(self) -> None:
        profile = _embedding_profile()
        self.assertEqual(
            profile.embedding_space_id,
            embedding_space_id(
                profile.provider,
                profile.model,
                profile.revision,
                profile.dimensions,
                profile.normalization,
            ),
        )
        self.assertEqual(profile, _embedding_profile())

        with self.assertRaises(ValueError):
            dataclasses.replace(profile, dimensions=0)
        with self.assertRaises(ValueError):
            dataclasses.replace(profile, provider=" ")

    def test_request_preserves_exact_contiguous_source_and_is_fingerprint_stable(
        self,
    ) -> None:
        request = _request()
        rebuilt = _request()

        self.assertEqual(request.normalized_text, "".join(seed.text for seed in request.chunks))
        self.assertEqual(request.request_fingerprint, rebuilt.request_fingerprint)
        self.assertEqual([seed.ordinal for seed in request.chunks], [0, 1, 2])
        self.assertEqual(
            [seed.char_start for seed in request.chunks],
            [0, request.chunks[0].char_end, request.chunks[1].char_end],
        )

        changed_space = dataclasses.replace(
            request,
            embedding_profile=dataclasses.replace(
                request.embedding_profile,
                revision="v2",
            ),
        )
        self.assertNotEqual(
            request.request_fingerprint,
            changed_space.request_fingerprint,
        )

    def test_request_rejects_ambiguous_source_layout_and_invalid_version_metadata(
        self,
    ) -> None:
        request = _request()
        gap = dataclasses.replace(
            request.chunks[1],
            char_start=request.chunks[1].char_start + 1,
            char_end=request.chunks[1].char_end + 1,
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                request,
                chunks=(request.chunks[0], gap, request.chunks[2]),
            )

        wrong_ordinal = dataclasses.replace(request.chunks[1], ordinal=9)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                request,
                chunks=(request.chunks[0], wrong_ordinal, request.chunks[2]),
            )

        with self.assertRaises(ValueError):
            dataclasses.replace(request, version_number=0)
        with self.assertRaises(ValueError):
            dataclasses.replace(request, source_generation=-1)
        with self.assertRaises(ValueError):
            dataclasses.replace(request, original_checksum="not-a-checksum")
        with self.assertRaises(ValueError):
            dataclasses.replace(
                request,
                ingested_at=datetime(2024, 1, 1),
            )


if __name__ == "__main__":
    unittest.main()
