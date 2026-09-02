"""Tests for the gold-independent deterministic answer prediction asset."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from graphrag_prod.evaluation.reference_predictions import (
    load_reference_predictions,
    prediction_payload,
)
from graphrag_prod.generation import AnswerModelRequest


ROOT = Path(__file__).parents[2]
PREDICTIONS = ROOT / "evaluation" / "reference-answer-predictions.v1.json"


class ReferencePredictionTests(unittest.TestCase):
    def test_asset_is_pinned_and_predictions_render_only_present_sources(self) -> None:
        predictions = load_reference_predictions(PREDICTIONS)
        self.assertEqual(len(predictions), 49)
        self.assertEqual(
            sum(item["status"] == "answered" for item in predictions.values()),
            35,
        )
        self.assertEqual(
            sum(
                item["status"] == "insufficient_context"
                for item in predictions.values()
            ),
            14,
        )

        question, prediction = next(
            (query, item)
            for query, item in predictions.items()
            if item["status"] == "answered" and not item["claims"][0]["inference"]
        )
        evidence_ids = list(
            dict.fromkeys(
                chunk_id
                for claim in prediction["claims"]
                for chunk_id in claim["evidence_chunk_ids"]
            )
        )
        sources = [
            {
                "chunk_id": chunk_id,
                "citation_id": f"S{index}",
                "text": f"authoritative source {index}",
            }
            for index, chunk_id in enumerate(evidence_ids, start=1)
        ]
        request = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps({"question": question, "sources": sources})
        )
        rendered = prediction_payload(request, predictions)
        self.assertEqual(rendered["status"], "answered")
        self.assertEqual(
            rendered["claims"][0]["citation_ids"],
            [
                sources[evidence_ids.index(chunk_id)]["citation_id"]
                for chunk_id in prediction["claims"][0]["evidence_chunk_ids"]
            ],
        )
        self.assertEqual(
            rendered["claims"][0]["evidence"][0]["quote"],
            sources[0]["text"],
        )

        missing = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps({"question": question, "sources": []})
        )
        self.assertEqual(
            prediction_payload(missing, predictions)["status"],
            "insufficient_context",
        )

        alternate_predictions = {
            "alternate source question": {
                "claims": [
                    {
                        "evidence_chunk_ids": ["chunk-a", "chunk-b"],
                        "inference": False,
                        "text": "A reviewed sourced claim.",
                    }
                ],
                "id": "alternate-source-case",
                "status": "answered",
            }
        }
        one_alternate = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps(
                {
                    "question": "alternate source question",
                    "sources": [
                        {
                            "chunk_id": "chunk-b",
                            "citation_id": "S1",
                            "text": "the available reviewed source",
                        }
                    ],
                }
            )
        )
        rendered_alternate = prediction_payload(
            one_alternate,
            alternate_predictions,
        )
        self.assertEqual(rendered_alternate["status"], "answered")
        self.assertEqual(rendered_alternate["claims"][0]["citation_ids"], ["S1"])

        no_alternate = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps(
                {"question": "alternate source question", "sources": []}
            )
        )
        self.assertEqual(
            prediction_payload(no_alternate, alternate_predictions)["status"],
            "insufficient_context",
        )

        boundary_query = "Atlas offers what for ATC, and what for ATL?"
        boundary_prediction = predictions[boundary_query]
        self.assertEqual(
            boundary_prediction["id"],
            "graph_relationship-boundary-02",
        )
        stage8_selected_ids = (
            "038d4090-bcc6-55af-b98e-d59f11ba1f05",
            "14bb23cb-10df-5335-a67e-be656165d34a",
            "4ba4952d-485b-5035-83ac-f159c9ac3869",
        )
        stage8_context = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps(
                {
                    "question": boundary_query,
                    "sources": [
                        {
                            "chunk_id": chunk_id,
                            "citation_id": f"S{index}",
                            "text": f"reviewed Stage 8 source {index}",
                        }
                        for index, chunk_id in enumerate(
                            stage8_selected_ids,
                            start=1,
                        )
                    ],
                }
            )
        )
        rendered_boundary = prediction_payload(stage8_context, predictions)
        self.assertEqual(rendered_boundary["status"], "answered")
        self.assertEqual(len(rendered_boundary["claims"]), 2)

        inference_predictions = {
            "inference question": {
                "claims": [
                    {
                        "evidence_chunk_ids": ["operand-a", "operand-b"],
                        "inference": True,
                        "text": "A reviewed inference.",
                    }
                ],
                "id": "inference-case",
                "status": "answered",
            }
        }
        missing_operand = AnswerModelRequest(
            prompt="INPUT_JSON:\n"
            + json.dumps(
                {
                    "question": "inference question",
                    "sources": [
                        {
                            "chunk_id": "operand-a",
                            "citation_id": "S1",
                            "text": "the first required operand",
                        }
                    ],
                }
            )
        )
        self.assertEqual(
            prediction_payload(missing_operand, inference_predictions)["status"],
            "insufficient_context",
        )

        raw = json.loads(PREDICTIONS.read_text(encoding="utf-8"))
        tampered = deepcopy(raw)
        tampered["version"] = "forged"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.json"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "do not match the pin"):
                load_reference_predictions(path)


if __name__ == "__main__":
    unittest.main()
