"""Offline graph pagination, evidence, trust and state-boundary checks."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from graphrag_prod.api.graph_contracts import GraphBrowseRequest, GraphBrowseResponse, GraphEvidenceResponseEnvelope
from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.browse_models import GraphBrowseQuery, GraphReadPin, GraphViewChanged, GraphBrowseLimitExceeded, GraphBrowseUnavailable, read_graph_state
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser, _View
from graphrag_prod.graph.view_tokens import GraphViewTokenCodec
from graphrag_prod.retrieval.models import VersionFilter
from graphrag_prod.retrieval.subgraph import Neo4jEvidenceSubgraphProjector
from tests.unit import test_retrieval_subgraph as fixture


PIN = GraphReadPin("publication-active-1", 1, 3, "tbox-industrial-v1", "a" * 64, 12)
PRINCIPAL = Principal("reader", fixture.TENANT, frozenset({"asset-engineers"}), frozenset({"knowledge:graph:read"}))
SCHEMA = SimpleNamespace(entity_types=(), relationship_types=(), hierarchies=())


class FakeGraphDriver:
    def __init__(self):
        self.mentions = deepcopy(fixture._mention_rows())
        self.assertions = deepcopy(fixture._assertion_rows())
        self.calls = []
        self.after = None

    def session(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute_read(self, work, *args):
        return work(self, *args)

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if self.after:
            self.after(len(self.calls))
        is_mention = "governed-subgraph:mentions" in query
        values = self.mentions if is_mention else self.assertions
        key = "mention" if is_mention else "assertion"
        return deepcopy([item for item in values if item[key]["authority_level"] in parameters["authority_levels"]
                         and parameters["tenant_id"] == fixture.TENANT and "asset-engineers" in parameters["groups"]
                         and (not parameters["version_ids"] or "version-1" in parameters["version_ids"])])


class GraphBrowsingTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeGraphDriver()
        self.browser = Neo4jPublishedGraphBrowser(self.driver, cursor_signing_key=b"a" * 32)
        self.boundary = patch("graphrag_prod.graph.browsing.read_graph_state", return_value=(PIN, SCHEMA))
        self.boundary.start()
        self.addCleanup(self.boundary.stop)

    def test_browse_preserves_parallel_trust_literal_and_exact_evidence(self):
        result = self.browser.query(PRINCIPAL, GraphBrowseQuery())
        parsed = GraphBrowseResponse.model_validate(result)
        self.assertEqual(len(parsed.nodes), 2)
        self.assertEqual(len(parsed.edges), 1)
        self.assertEqual(parsed.literals[0].value, "12 bar")
        pump = next(item for item in parsed.nodes if item.label == "Pump-7")
        self.assertEqual(pump.authority_levels, ("AUTHORITATIVE", "SECONDARY"))
        self.assertEqual(len(pump.mention_revision_ids), 2)
        evidence = self.browser.evidence(PRINCIPAL, ("owns-published", "pressure-published", "missing"), view_token=result["view_token"])
        parsed_evidence = GraphEvidenceResponseEnvelope.model_validate(evidence)
        self.assertEqual(len(parsed_evidence.items), 2)
        self.assertEqual(parsed_evidence.items[0].evidence.citation.chunk_text, fixture.CHUNK_TEXT)
        self.assertEqual(parsed_evidence.items[0].applicability.sme_review_state, "NOT_RECORDED")

    def test_typed_chinese_literal_and_source_text_roundtrip_through_browser_api(self):
        from graphrag_prod.domain.models import TypedLiteralValue
        text = "Pump-7 对应项目模拟配置。"
        semantics = TypedLiteralValue(datatype="STRING", typed_value="项目模拟配置",
                                      raw_value="项目模拟配置", canonical_value="项目模拟配置")
        self.driver.mentions, self.driver.assertions = fixture._literal_source_rows(text, semantics.raw_value, semantics)
        result = self.browser.query(PRINCIPAL, GraphBrowseQuery())
        parsed = GraphBrowseResponse.model_validate(result)
        self.assertEqual(parsed.literals[0].value, semantics.raw_value)
        self.assertEqual(parsed.literals[0].semantics.datatype, "STRING")
        evidence = self.browser.evidence(PRINCIPAL, ("pressure-published",), view_token=result["view_token"])
        parsed_evidence = GraphEvidenceResponseEnvelope.model_validate(evidence)
        self.assertEqual(parsed_evidence.items[0].evidence.quoted_text, text)
        self.assertEqual(parsed_evidence.items[0].evidence.citation.chunk_text, text)

    def test_cursor_has_no_skips_or_repeated_assertion_ids_and_binds_query(self):
        query = GraphBrowseQuery(page_size=1)
        result = self.browser.query(PRINCIPAL, query)
        ids = []
        while True:
            GraphBrowseResponse.model_validate(result)
            ids.extend(item["revision_id"] for item in (*result["edges"], *result["literals"]))
            if not result["page"]["has_more"]:
                break
            previous = result
            result = self.browser.query(PRINCIPAL, query, view_token=result["view_token"], cursor=result["page"]["next_cursor"])
        self.assertEqual(sorted(ids), ["owns-published", "pressure-published"])
        with self.assertRaises(GraphViewChanged):
            self.browser.query(PRINCIPAL, replace(query, page_size=2), view_token=previous["view_token"], cursor=previous["page"]["next_cursor"])

    def test_predicate_filters_drop_unrelated_nodes_and_unknown_seed_has_no_fallback(self):
        empty = self.browser.query(PRINCIPAL, GraphBrowseQuery(predicates=("MISSING",)))
        self.assertFalse(empty["nodes"])
        self.assertFalse(empty["edges"])
        connected = self.browser.query(PRINCIPAL, GraphBrowseQuery(predicates=("OWNS",)))
        self.assertEqual(len(connected["nodes"]), 2)
        self.assertFalse(connected["literals"])
        unknown = self.browser.query(PRINCIPAL, GraphBrowseQuery(seed_entity_ids=("unknown",)))
        self.assertFalse(unknown["nodes"])

    def test_direction_and_secondary_filter_apply_to_expansion(self):
        query = GraphBrowseQuery(seed_entity_ids=(fixture.PUMP["entity_id"],), direction="incoming", predicates=("OWNS",))
        incoming = self.browser.query(PRINCIPAL, query)
        self.assertEqual(len(incoming["edges"]), 1)
        outgoing = self.browser.query(PRINCIPAL, replace(query, direction="outgoing"))
        self.assertFalse(outgoing["edges"])
        primary = self.browser.query(PRINCIPAL, GraphBrowseQuery(trust_policy="AUTHORITATIVE_ONLY"))
        self.assertFalse(primary["literals"])
        self.assertTrue(all(item["authority_levels"] == ("AUTHORITATIVE",) for item in primary["nodes"]))

    def test_empty_scope_short_circuits_graph_rows_and_keeps_operational_pin(self):
        result = self.browser.query(PRINCIPAL, GraphBrowseQuery(), version_filter=VersionFilter(match_none=True))
        self.assertFalse(self.driver.calls)
        self.assertFalse(result["nodes"])
        self.assertEqual(result["pin"]["activation_generation"], 3)

    def test_view_rejects_publication_aba_tbox_corpus_and_acl_changes(self):
        initial = self.browser.query(PRINCIPAL, GraphBrowseQuery())
        for changed in (replace(PIN, activation_generation=4), replace(PIN, corpus_revision=13), replace(PIN, tbox_checksum="b" * 64)):
            with self.subTest(changed=changed), patch("graphrag_prod.graph.browsing.read_graph_state", return_value=(changed, SCHEMA)):
                with self.assertRaises(GraphViewChanged):
                    self.browser.evidence(PRINCIPAL, ("owns-published",), view_token=initial["view_token"])
        self.driver.mentions = self.driver.mentions[:2]
        self.driver.assertions = self.driver.assertions[:1]
        with self.assertRaises(GraphViewChanged):
            self.browser.query(PRINCIPAL, GraphBrowseQuery(), view_token=initial["view_token"])

    def test_same_read_acl_changes_reject_before_return(self):
        def revoke(call):
            if call == 3:
                self.driver.mentions = ()
                self.driver.assertions = ()
        self.driver.after = revoke
        with self.assertRaises(GraphViewChanged):
            self.browser.query(PRINCIPAL, GraphBrowseQuery())

    def test_tampered_or_cross_identity_tokens_fail_and_reader_capability_is_required(self):
        result = self.browser.query(PRINCIPAL, GraphBrowseQuery())
        token = result["view_token"]
        for principal, value in ((replace(PRINCIPAL, principal_id="other"), token), (PRINCIPAL, token[:-1] + ("0" if token[-1] != "0" else "1"))):
            with self.assertRaises(GraphViewChanged):
                self.browser.evidence(principal, ("owns-published",), view_token=value)
        with self.assertRaises(PermissionError):
            self.browser.query(replace(PRINCIPAL, capabilities=frozenset()), GraphBrowseQuery())

    def test_projection_rejects_spoofed_ranges_and_record_count_overflow(self):
        self.driver.mentions[0]["mention"]["evidence_text"] = "fake"
        with self.assertRaises(GraphBrowseUnavailable):
            self.browser.query(PRINCIPAL, GraphBrowseQuery())
        self.driver.mentions = tuple(deepcopy(fixture._mention_rows()[0]) for _ in range(501))
        with self.assertRaises(GraphBrowseLimitExceeded):
            self.browser.query(PRINCIPAL, GraphBrowseQuery())

    def test_dto_rejects_extra_identity_bad_depth_and_duplicate_seeds(self):
        for values in ({"tenant_id": "victim"}, {"hops": 3}, {"hops": True}, {"seed_entity_ids": ["same", "same"]}, {"name_query": "a\nb"}, {"cursor": "x"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                GraphBrowseRequest.model_validate(values)

    def test_token_purpose_expiry_and_principal_groups_are_bound(self):
        now = [1000.0]
        codec = GraphViewTokenCodec(b"a" * 32, clock=lambda: now[0], lifetime_seconds=60)
        token = codec.encode(PRINCIPAL, "view", {"key": "value"})
        self.assertEqual(codec.decode(PRINCIPAL, "view", token)[0], {"key": "value"})
        for purpose, who in (("cursor", PRINCIPAL), ("view", replace(PRINCIPAL, groups=frozenset({"different"})))):
            with self.assertRaises(GraphViewChanged):
                codec.decode(who, purpose, token)
        now[0] = 1060
        with self.assertRaises(GraphViewChanged):
            codec.decode(PRINCIPAL, "view", token)

    def test_large_visible_star_paginates_with_endpoint_closure_under_hard_bounds(self):
        def node(key):
            return {"entity_id": key, "entity_type": "Asset", "label": key, "canonical_key": key,
                    "authority_levels": ("SECONDARY",), "mention_revision_ids": ("mention-" + key,)}
        nodes = {key: node(key) for key in ("hub", *(f"leaf-{i:03d}" for i in range(200)))}
        assertions = {}
        evidence = {}
        provenance = SimpleNamespace(authority=SimpleNamespace(value="SECONDARY"), origin=SimpleNamespace(value="RULE_DERIVED"), confidence=0.9)
        for i in range(200):
            key = f"edge-{i:03d}"
            assertions[key] = SimpleNamespace(subject_entity_id="hub", object_entity_id=f"leaf-{i:03d}", predicate="CONNECTS_TO",
                record_id="record-" + key, evidence=SimpleNamespace(provenance=provenance))
            evidence[key] = {"source_kind": None}
        view = _View(PIN, {"entity_types": (), "relationship_types": (), "hierarchies": ()}, nodes, assertions, evidence, "a" * 64)
        query = GraphBrowseQuery(seed_entity_ids=("hub",), predicates=("CONNECTS_TO",), page_size=200)
        seen = []
        with patch.object(self.browser, "_load", return_value=view):
            page = self.browser.query(PRINCIPAL, query)
            self.assertEqual(len(page["nodes"]), 150)
            while True:
                GraphBrowseResponse.model_validate(page)
                seen.extend(item["revision_id"] for item in page["edges"])
                if not page["page"]["has_more"]:
                    break
                page = self.browser.query(PRINCIPAL, query, view_token=page["view_token"], cursor=page["page"]["next_cursor"])
        self.assertEqual(len(seen), 200)
        self.assertEqual(len(set(seen)), 200)

    def test_pin_reader_checks_actual_bound_tbox_checksum_and_tenant(self):
        from graphrag_prod.industrial.ontology import build_industrial_tbox
        tbox = build_industrial_tbox(fixture.TENANT).with_status("PUBLISHED")
        record = {"tbox_id": tbox.tbox_id, "tenant_id": tbox.tenant_id, "key": tbox.key,
                  "version": tbox.version, "status": "PUBLISHED", "checksum": tbox.checksum, "definition_json": json.dumps(tbox.to_mapping())}
        row = {"corpus_revision": 12, "activation_generation": 3, "publication": {"publication_id": "pub", "generation": 1,
                "tenant_id": fixture.TENANT, "status": "ACTIVE", "ontology_version_id": tbox.tbox_id}, "active_links": 1, "tbox_links": 1, "tboxes": [record]}
        tx = SimpleNamespace(run=lambda *args, **kwargs: [row])
        pin, loaded = read_graph_state(tx, fixture.TENANT)
        self.assertEqual(pin.tbox_checksum, tbox.checksum)
        self.assertEqual(loaded.hierarchies, tbox.hierarchies)
        record["checksum"] = "b" * 64
        with self.assertRaises(GraphViewChanged):
            read_graph_state(tx, fixture.TENANT)

    def test_pinned_projection_rechecks_selected_sources_without_graph_records(self):
        driver = FakeGraphDriver()
        original = driver.run
        calls = []

        def run(query, **parameters):
            if "governed-subgraph:selected-sources" in query:
                calls.append(parameters)
                return [{"chunk_id": key} for key in sorted(parameters["chunk_ids"])]
            return original(query, **parameters)

        driver.run = run
        projector = Neo4jEvidenceSubgraphProjector(driver)
        with patch("graphrag_prod.retrieval.subgraph.read_graph_state", return_value=(PIN, SCHEMA)):
            graph = projector.project(PRINCIPAL, (fixture.CHUNK_ID, "no-graph"), expected_pin=PIN)
        self.assertTrue(graph.entities)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(set(item["chunk_ids"]) == {fixture.CHUNK_ID, "no-graph"} for item in calls))
        self.assertEqual(calls[0]["selected_source_limit"], 3)

    def test_pinned_projection_rejects_before_or_during_source_revocation_even_without_publication(self):
        for empty_publication in (False, True):
            for revoke_at in (1, 2):
                with self.subTest(empty_publication=empty_publication, revoke_at=revoke_at):
                    driver = FakeGraphDriver()
                    original = driver.run
                    calls = []

                    def run(query, **parameters):
                        if "governed-subgraph:selected-sources" in query:
                            calls.append(1)
                            ids = parameters["chunk_ids"] if len(calls) < revoke_at else (fixture.CHUNK_ID,)
                            return [{"chunk_id": key} for key in ids]
                        return original(query, **parameters)

                    driver.run = run
                    pin = GraphReadPin(None, 0, 0, None, None, 12) if empty_publication else PIN
                    with patch("graphrag_prod.retrieval.subgraph.read_graph_state", return_value=(pin, SCHEMA)):
                        with self.assertRaises(GraphViewChanged):
                            Neo4jEvidenceSubgraphProjector(driver).project(PRINCIPAL, (fixture.CHUNK_ID, "no-graph"), expected_pin=pin)


if __name__ == "__main__":
    unittest.main()
