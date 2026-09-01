"""Offline checks for the deterministic Stage 5A development corpus."""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from graphrag_prod.domain import (
    chunk_id,
    content_checksum,
    document_id,
    entity_id,
    version_id,
)
from scripts.build_dev_corpus import (
    DEFAULT_DATASET_DIR,
    EMBEDDING_DIMENSIONS,
    SPLITTER_SIGNATURE,
    build_dataset,
    check_dataset,
    write_dataset,
)


def _json_lines(payload: bytes) -> list[dict]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]


class DevelopmentCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = build_dataset()

    def test_checked_in_dataset_matches_the_deterministic_builder(self) -> None:
        self.assertEqual(check_dataset(DEFAULT_DATASET_DIR, self.build), ())

    def test_manifest_declares_representative_but_non_production_coverage(self) -> None:
        manifest = self.build.manifest
        self.assertTrue(manifest["synthetic"])
        self.assertIn("not production", manifest["warning"].casefold())
        self.assertEqual(manifest["counts"]["active_chunks"], 120)
        self.assertEqual(manifest["counts"]["documents"], 10)
        self.assertEqual(manifest["counts"]["companies"], 5)
        self.assertEqual(manifest["counts"]["tenants"], 2)
        self.assertEqual(manifest["coverage"]["fiscal_years"], [2023, 2024])
        self.assertGreaterEqual(len(manifest["coverage"]["access_groups"]), 4)
        self.assertEqual(manifest["embedding_profile"]["quality_claim"], "none")
        self.assertIn(
            "embedding model quality",
            manifest["fixture_vector_scope"]["cannot_validate"],
        )

    def test_sources_round_trip_to_contiguous_stable_chunks(self) -> None:
        chunks_by_document: dict[str, list[dict]] = {}
        for chunk in self.build.chunks:
            chunks_by_document.setdefault(chunk["document_key"], []).append(chunk)
        for document in self.build.documents:
            with self.subTest(document=document["document_key"]):
                source = self.build.files[document["source_path"]].decode("utf-8")
                self.assertEqual(content_checksum(source), document["checksum"])
                self.assertEqual(
                    document_id(document["tenant_id"], document["canonical_uri"]),
                    document["document_id"],
                )
                self.assertEqual(
                    version_id(
                        document["document_id"],
                        document["checksum"],
                        document["original_checksum"],
                    ),
                    document["version_id"],
                )
                cursor = 0
                chunks = sorted(
                    chunks_by_document[document["document_key"]],
                    key=lambda item: item["ordinal"],
                )
                self.assertEqual(len(chunks), 12)
                for ordinal, chunk in enumerate(chunks):
                    self.assertEqual(chunk["ordinal"], ordinal)
                    self.assertEqual(chunk["char_start"], cursor)
                    text = source[chunk["char_start"] : chunk["char_end"]]
                    self.assertEqual(content_checksum(text), chunk["checksum"])
                    self.assertIn(document["company_canonical_name"], text)
                    self.assertIn(f"({document['ticker']})", text)
                    self.assertIn(f"fiscal year {document['fiscal_year']}", text)
                    if ordinal > 0:
                        self.assertTrue(text.startswith(document["identity_anchor"]))
                    self.assertEqual(
                        chunk_id(
                            document["version_id"],
                            SPLITTER_SIGNATURE,
                            ordinal,
                            chunk["char_start"],
                            chunk["char_end"],
                            chunk["checksum"],
                        ),
                        chunk["chunk_id"],
                    )
                    for mention in chunk["mentions"]:
                        self.assertEqual(
                            source[mention["char_start"] : mention["char_end"]],
                            mention["surface"],
                        )
                    company_mentions = [
                        mention
                        for mention in chunk["mentions"]
                        if mention["entity_id"] == document["company_entity_id"]
                    ]
                    self.assertEqual(len(company_mentions), 1)
                    self.assertEqual(
                        company_mentions[0]["surface"],
                        document["company_canonical_name"],
                    )
                    cursor = chunk["char_end"]
                self.assertEqual(cursor, len(source))

    def test_homonym_pair_is_same_surface_but_distinct_company_identity(self) -> None:
        pair = self.build.manifest["coverage"]["homonym_negative_pairs"][0]
        self.assertEqual(pair["shared_surface"], "Atlas")
        self.assertEqual(pair["expected_resolution"], "KEEP_SEPARATE")
        self.assertEqual(len(set(pair["entity_ids"])), 2)
        entities = {item["entity_key"]: item for item in self.build.entities}
        first, second = (entities[key] for key in pair["entity_keys"])
        self.assertEqual(first["tenant_id"], second["tenant_id"])
        self.assertEqual(first["entity_type"], "Company")
        self.assertEqual(second["entity_type"], "Company")
        self.assertIn("Atlas", first["aliases"])
        self.assertIn("Atlas", second["aliases"])
        self.assertNotEqual(first["canonical_key"], second["canonical_key"])
        for item in (first, second):
            self.assertEqual(
                entity_id(item["tenant_id"], item["entity_type"], item["canonical_key"]),
                item["entity_id"],
            )

    def test_questions_cover_every_class_and_enforce_acl_expectations(self) -> None:
        quotas = Counter(
            (item["question_class"], item["case_type"])
            for item in self.build.questions
        )
        self.assertEqual(len(self.build.questions), 49)
        for question_class in self.build.manifest["coverage"]["question_classes"]:
            self.assertEqual(quotas[(question_class, "success")], 5)
            self.assertEqual(quotas[(question_class, "boundary")], 2)

        chunks = {item["chunk_id"]: item for item in self.build.chunks}
        unauthorized = [
            item
            for item in self.build.questions
            if item["question_class"] == "unauthorized"
        ]
        self.assertEqual(len(unauthorized), 7)
        for item in unauthorized:
            self.assertFalse(item["answerable"])
            self.assertTrue(item["forbidden_chunk_ids"])
            principal = item["principal"]
            for forbidden_id in item["forbidden_chunk_ids"]:
                chunk = chunks[forbidden_id]
                same_tenant = chunk["tenant_id"] == principal["tenant_id"]
                group_overlap = bool(
                    set(chunk["access_groups"]) & set(principal["groups"])
                )
                self.assertFalse(same_tenant and group_overlap)

    def test_gold_includes_all_equivalent_fact_evidence_without_temporal_leakage(self) -> None:
        questions = {item["id"]: item for item in self.build.questions}

        offering = set(
            questions["graph_relationship-success-01"]["relevance_chunk_keys"]
        )
        self.assertEqual(
            offering,
            {
                "tenant-alpha:nst:fy2023:business",
                "tenant-alpha:nst:fy2023:product",
                "tenant-alpha:nst:fy2024:business",
                "tenant-alpha:nst:fy2024:product",
            },
        )

        explicit_year_segments = set(
            questions["cross_chunk-success-05"]["relevance_chunk_keys"]
        )
        self.assertEqual(len(explicit_year_segments), 4)
        self.assertTrue(
            all(":fy2024:" in key for key in explicit_year_segments)
        )
        self.assertTrue(
            all(
                key.endswith(":segment") or key.endswith(":segment-detail")
                for key in explicit_year_segments
            )
        )

        temporal = questions["temporal_conflict-boundary-01"]
        positive = {
            key
            for key, grade in temporal["relevance_chunk_keys"].items()
            if grade > 0
        }
        excluded = {
            key
            for key, grade in temporal["relevance_chunk_keys"].items()
            if grade == 0
        }
        self.assertEqual(positive, {"tenant-alpha:nst:fy2024:revenue"})
        self.assertEqual(excluded, {"tenant-alpha:nst:fy2023:revenue"})

        forbidden = set(
            questions["unauthorized-success-02"]["forbidden_chunk_keys"]
        )
        self.assertEqual(
            forbidden,
            {
                "tenant-alpha:atl:fy2023:business",
                "tenant-alpha:atl:fy2023:product",
                "tenant-alpha:atl:fy2024:business",
                "tenant-alpha:atl:fy2024:product",
            },
        )

    def test_fixture_vectors_are_stable_finite_and_nonzero(self) -> None:
        replay = build_dataset()
        self.assertEqual(self.build.files, replay.files)
        self.assertEqual(len(self.build.vectors), 169)
        for record in self.build.vectors:
            vector = record["vector"]
            self.assertEqual(len(vector), EMBEDDING_DIMENSIONS)
            self.assertTrue(all(math.isfinite(value) for value in vector))
            self.assertGreater(math.sqrt(sum(value * value for value in vector)), 0.0)

        vectors = {item["id"]: item for item in self.build.vectors}
        chunks = {item["chunk_key"]: item for item in self.build.chunks}
        business = chunks["tenant-alpha:nst:fy2024:business"]
        product = chunks["tenant-alpha:nst:fy2024:product"]
        self.assertEqual(business["semantic_cluster"], product["semantic_cluster"])
        self.assertEqual(
            vectors[business["chunk_id"]]["vector"],
            vectors[product["chunk_id"]]["vector"],
        )

        chunk_features = {
            feature
            for chunk in self.build.chunks
            for feature in vectors[chunk["chunk_id"]]["semantic_features"]
        }
        unanswerable = next(
            item
            for item in self.build.questions
            if item["id"] == "unanswerable-success-01"
        )
        query_features = set(
            vectors[unanswerable["vector_id"]]["semantic_features"]
        )
        self.assertTrue(query_features)
        self.assertTrue(query_features.isdisjoint(chunk_features))

    def test_write_and_check_detect_byte_drift_without_deleting_unknown_files(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "dev-corpus-v1"
            write_dataset(target, self.build)
            self.assertEqual(check_dataset(target, self.build), ())

            manifest_path = target / "manifest.json"
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
            self.assertIn(
                "generated file drifted: manifest.json",
                check_dataset(target, self.build),
            )

            manifest_path.write_bytes(self.build.files["manifest.json"])
            unknown = target / "manual-note.txt"
            unknown.write_text("preserve me", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to remove"):
                write_dataset(target, self.build)
            self.assertEqual(unknown.read_text(encoding="utf-8"), "preserve me")

    def test_generated_jsonl_counts_match_manifest(self) -> None:
        manifest = self.build.manifest
        self.assertEqual(
            len(_json_lines(self.build.files["chunks.jsonl"])),
            manifest["counts"]["active_chunks"],
        )
        self.assertEqual(
            len(_json_lines(self.build.files["entities.jsonl"])),
            manifest["counts"]["entities"],
        )
        self.assertEqual(
            len(_json_lines(self.build.files["questions.jsonl"])),
            manifest["counts"]["questions"],
        )
        self.assertEqual(
            len(_json_lines(self.build.files["vectors.jsonl"])),
            manifest["counts"]["vectors"],
        )


if __name__ == "__main__":
    unittest.main()
