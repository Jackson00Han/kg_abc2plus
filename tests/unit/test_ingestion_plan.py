"""Pure ingestion-plan invariants with deterministic multi-chunk fixtures."""

from __future__ import annotations

import dataclasses
import unittest

from graphrag_prod.ingestion.models import (
    IngestionPlan,
    _fingerprint,
    default_artifact_input_hash,
)
from tests.fixtures.ingestion import (
    CHUNKS_V2,
    FIXED_TIME,
    make_bundles,
    make_plan,
    make_profile,
)


def _build(bundles: tuple, operation_key: str = "unit-plan") -> IngestionPlan:
    return IngestionPlan.build(
        operation_key=operation_key,
        profile=make_profile(),
        bundles=bundles,
        expected_active_snapshot_id=None,
        source_generation=0,
        artifact_input_hashes={
            bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
            for bundle in bundles
        },
        created_at=FIXED_TIME,
    )


class IngestionPlanTests(unittest.TestCase):
    def test_build_seals_a_complete_staging_snapshot(self) -> None:
        plan = make_plan()
        rebuilt = make_plan()

        self.assertEqual(plan, rebuilt)
        self.assertEqual(len(plan.bundles), 3)
        self.assertEqual(plan.snapshot.expected_chunk_count, 3)
        self.assertEqual(plan.snapshot.version_id, plan.version_id)
        self.assertEqual(plan.snapshot.profile_id, plan.profile.profile_id)
        self.assertTrue(all(not bundle.activate_version for bundle in plan.bundles))
        self.assertEqual(
            {chunk_id for chunk_id, _ in plan.artifact_input_hashes},
            {bundle.chunk.chunk_id for bundle in plan.bundles},
        )
        self.assertTrue(
            all(
                embedding.vector_checksum
                for bundle in plan.bundles
                for embedding in bundle.all_embeddings
            )
        )
        self.assertIsNone(plan.bundles[1].embedding)
        self.assertIsNone(plan.bundles[1].assertion)
        self.assertEqual(len(plan.bundles[1].additional_embeddings), 1)
        self.assertEqual(len(plan.bundles[1].additional_assertions), 1)

        reversed_plan = _build(tuple(reversed(plan.bundles)), "reversed-order")
        self.assertEqual(reversed_plan.snapshot.snapshot_id, plan.snapshot.snapshot_id)
        self.assertEqual(reversed_plan.snapshot.manifest_hash, plan.snapshot.manifest_hash)

    def test_plan_rejects_incomplete_or_mixed_membership(self) -> None:
        plan = make_plan()
        with self.assertRaisesRegex(ValueError, "every chunk"):
            dataclasses.replace(
                plan,
                artifact_input_hashes=plan.artifact_input_hashes[:-1],
            )

        activated = (
            dataclasses.replace(plan.bundles[0], activate_version=True),
            *plan.bundles[1:],
        )
        with self.assertRaisesRegex(ValueError, "must not activate"):
            _build(activated, "activated")

        v2 = make_bundles(chunk_specs=CHUNKS_V2, version_number=2)
        mixed_versions = (plan.bundles[0], v2[1])
        with self.assertRaisesRegex(ValueError, "tenant/document/version"):
            _build(mixed_versions, "mixed-version")

        with self.assertRaisesRegex(ValueError, "duplicate chunk"):
            _build((plan.bundles[0], plan.bundles[0]), "duplicate-chunk")

    def test_plan_rejects_mixed_document_access_snapshots(self) -> None:
        plan = make_plan()
        changed_groups = frozenset({"legal-readers"})
        mixed_bundle = dataclasses.replace(
            plan.bundles[1],
            document=dataclasses.replace(
                plan.bundles[1].document,
                access_policy_version=2,
                access_groups=changed_groups,
            ),
            chunk=dataclasses.replace(
                plan.bundles[1].chunk,
                access_policy_version=2,
                access_groups=changed_groups,
            ),
        )
        bundles = (plan.bundles[0], mixed_bundle, plan.bundles[2])

        with self.assertRaises(ValueError):
            _build(bundles, "mixed-document-policy")

    def test_partial_update_reuses_only_unchanged_artifact_inputs(self) -> None:
        v1 = make_plan()
        v2 = make_plan(
            operation_key="upsert-apple-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
        )
        v1_hashes = dict(v1.artifact_input_hashes)
        v2_hashes = dict(v2.artifact_input_hashes)
        v1_by_ordinal = {
            bundle.chunk.ordinal: v1_hashes[bundle.chunk.chunk_id]
            for bundle in v1.bundles
        }
        v2_by_ordinal = {
            bundle.chunk.ordinal: v2_hashes[bundle.chunk.chunk_id]
            for bundle in v2.bundles
        }

        self.assertNotEqual(v1.snapshot.snapshot_id, v2.snapshot.snapshot_id)
        self.assertTrue(
            all(
                left.chunk.chunk_id != right.chunk.chunk_id
                for left, right in zip(v1.bundles, v2.bundles, strict=True)
            )
        )
        self.assertEqual(v1_by_ordinal[0], v2_by_ordinal[0])
        self.assertNotEqual(v1_by_ordinal[1], v2_by_ordinal[1])
        self.assertEqual(v1_by_ordinal[2], v2_by_ordinal[2])

    def test_vector_order_is_part_of_request_and_artifact_fingerprints(self) -> None:
        forward = make_plan(operation_key="same-key")
        reversed_vectors = make_plan(
            operation_key="same-key",
            reverse_vectors=True,
        )

        self.assertEqual(forward.snapshot.snapshot_id, reversed_vectors.snapshot.snapshot_id)
        self.assertNotEqual(
            forward.request_fingerprint,
            reversed_vectors.request_fingerprint,
        )
        self.assertNotEqual(
            _fingerprint({"vector": [0.1, 0.2, 0.3, 0.4]}),
            _fingerprint({"vector": [0.4, 0.3, 0.2, 0.1]}),
        )

    def test_snapshot_manifest_seals_material_chunk_and_derivation_metadata(
        self,
    ) -> None:
        base = make_plan(operation_key="manifest-material-base")
        first = base.bundles[0]
        entity = first.entities[0]
        mention = first.mentions[0]
        assertion = first.all_assertions[0]
        renamed_entities = tuple(
            dataclasses.replace(
                bundle,
                entities=tuple(
                    dataclasses.replace(
                        item,
                        canonical_name="Apple Incorporated",
                    )
                    if item.entity_id == entity.entity_id
                    else item
                    for item in bundle.entities
                ),
            )
            for bundle in base.bundles
        )
        aliased_entities = tuple(
            dataclasses.replace(
                bundle,
                entities=tuple(
                    dataclasses.replace(
                        item,
                        aliases=("Apple", "Apple Computer"),
                    )
                    if item.entity_id == entity.entity_id
                    else item
                    for item in bundle.entities
                ),
            )
            for bundle in base.bundles
        )
        mutations = {
            "chunk-page": (
                dataclasses.replace(
                    first,
                    chunk=dataclasses.replace(first.chunk, page_number=2),
                ),
                *base.bundles[1:],
            ),
            "chunk-section": (
                dataclasses.replace(
                    first,
                    chunk=dataclasses.replace(first.chunk, section="Governed metric"),
                ),
                *base.bundles[1:],
            ),
            "entity-canonical-name": renamed_entities,
            "entity-aliases": aliased_entities,
            "mention-confidence": (
                dataclasses.replace(
                    first,
                    mentions=(dataclasses.replace(mention, confidence=0.75),),
                ),
                *base.bundles[1:],
            ),
            "assertion-confidence": (
                dataclasses.replace(
                    first,
                    assertion=dataclasses.replace(assertion, confidence=0.75),
                ),
                *base.bundles[1:],
            ),
            "assertion-accepted": (
                dataclasses.replace(
                    first,
                    assertion=dataclasses.replace(assertion, accepted=False),
                ),
                *base.bundles[1:],
            ),
        }

        for label, bundles in mutations.items():
            with self.subTest(label=label):
                mutated = _build(bundles, f"manifest-material-{label}")
                reordered = _build(
                    tuple(reversed(bundles)),
                    f"manifest-material-{label}-reordered",
                )

                self.assertEqual(mutated.snapshot.snapshot_id, base.snapshot.snapshot_id)
                self.assertNotEqual(
                    mutated.snapshot.manifest_hash,
                    base.snapshot.manifest_hash,
                )
                self.assertEqual(
                    reordered.snapshot.manifest_hash,
                    mutated.snapshot.manifest_hash,
                )


if __name__ == "__main__":
    unittest.main()
