"""Conservative authoritative-identity resolution and ACL query tests."""

from __future__ import annotations

import unittest

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.graph.governance import normalized_name_key
from graphrag_prod.knowledge.entity_resolution import (
    AuthoritativeEntityProfile,
    AuthoritativeEvidence,
    EntityResolutionService,
    ExactAuthoritativeMatch,
    IdentityPropertyValue,
    Neo4jAuthoritativeEntitySource,
    ResolutionBoundaryError,
    ResolutionOutcome,
    ResolutionPolicy,
)
from graphrag_prod.knowledge.models import EntityIdentity
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus
from graphrag_prod.ontology.models import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    TBoxStatus,
    TBoxVersion,
)


TENANT = "tenant-industrial"


def _tbox() -> TBoxVersion:
    return TBoxVersion(
        tenant_id=TENANT,
        key="plant-assets",
        version=1,
        status=TBoxStatus.PUBLISHED,
        entity_types=(EntityTypeDefinition("Asset", ("asset-id",)),),
        relationship_types=(),
    )


def _identity(
    canonical_key: str,
    canonical_name: str,
    *,
    aliases: tuple[str, ...] = (),
    tenant_id: str = TENANT,
    entity_type: str = "Asset",
) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id(tenant_id, entity_type, canonical_key),
        tenant_id=tenant_id,
        entity_type=entity_type,
        canonical_key=canonical_key,
        canonical_name=canonical_name,
        aliases=aliases,
    )


def _profile(identity: EntityIdentity, *, suffix: str) -> AuthoritativeEntityProfile:
    evidence_text = identity.aliases[0] if identity.aliases else identity.canonical_name
    return AuthoritativeEntityProfile(
        entity=identity,
        ontology_version_id=_tbox().tbox_id,
        authority=AuthorityLevel.AUTHORITATIVE,
        status=GovernanceStatus.PUBLISHED,
        evidence=(
            AuthoritativeEvidence(
                mention_revision_id=f"mention-{suffix}",
                document_id=f"document-{suffix}",
                version_id=f"version-{suffix}",
                chunk_id=f"chunk-{suffix}",
                char_start=0,
                char_end=len(evidence_text),
                quoted_text=evidence_text,
            ),
        ),
    )


class FakeSource:
    def __init__(self, targets: tuple[AuthoritativeEntityProfile, ...]) -> None:
        self.targets = targets
        self.calls: list[dict[str, object]] = []

    def find_exact_canonical_key(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        canonical_key: str,
    ) -> ExactAuthoritativeMatch:
        self.calls.append(
            {
                "kind": "exact-key",
                "principal": principal,
                "ontology_version_id": ontology_version_id,
                "entity_type": entity_type,
                "canonical_key": canonical_key,
            }
        )
        matches = tuple(
            target
            for target in self.targets
            if target.entity.canonical_key == canonical_key
        )
        return ExactAuthoritativeMatch(
            match_count=len(matches),
            target=matches[0] if len(matches) == 1 else None,
            matched_target_value=canonical_key if len(matches) == 1 else None,
        )

    def find_exact_governed_alias(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        candidate_values: tuple[str, ...],
    ) -> ExactAuthoritativeMatch:
        self.calls.append(
            {
                "kind": "exact-alias",
                "principal": principal,
                "ontology_version_id": ontology_version_id,
                "entity_type": entity_type,
                "candidate_values": candidate_values,
            }
        )
        keys = {normalized_name_key(value) for value in candidate_values}
        matches = tuple(
            (target, alias)
            for target in self.targets
            for alias in target.entity.aliases
            if normalized_name_key(alias) in keys
        )
        unique = {target.entity.entity_id: (target, alias) for target, alias in matches}
        selected = next(iter(unique.values())) if len(unique) == 1 else None
        return ExactAuthoritativeMatch(
            match_count=len(unique),
            target=None if selected is None else selected[0],
            matched_target_value=None if selected is None else selected[1],
        )

    def list_authoritative_entities(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        limit: int,
    ) -> tuple[AuthoritativeEntityProfile, ...]:
        self.calls.append(
            {
                "kind": "fuzzy",
                "principal": principal,
                "ontology_version_id": ontology_version_id,
                "entity_type": entity_type,
                "limit": limit,
            }
        )
        return self.targets[:limit]

    def find_exact_identity_properties(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        identity_properties: tuple[IdentityPropertyValue, ...],
    ) -> ExactAuthoritativeMatch:
        self.calls.append(
            {
                "kind": "exact-identity-properties",
                "principal": principal,
                "ontology_version_id": ontology_version_id,
                "entity_type": entity_type,
                "identity_properties": identity_properties,
            }
        )
        matches = tuple(
            target
            for target in self.targets
            if target.entity.canonical_key.endswith(
                identity_properties[0].canonical_value
            )
        )
        signature = (
            f"serial_number=STRING:{identity_properties[0].canonical_value}"
        )
        return ExactAuthoritativeMatch(
            match_count=len(matches),
            target=matches[0] if len(matches) == 1 else None,
            matched_target_value=signature if len(matches) == 1 else None,
        )


def _principal() -> Principal:
    return Principal("engineer-1", TENANT, frozenset({"asset-engineers"}))


class EntityResolutionTests(unittest.TestCase):
    def test_declared_identity_properties_auto_link_unique_authority(self) -> None:
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="plant-assets",
            version=2,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Asset",
                    ("asset-id",),
                    properties=(
                        PropertyDefinition(
                            "serial_number",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                    ),
                    identity_properties=("serial_number",),
                ),
            ),
            relationship_types=(),
        )
        target = AuthoritativeEntityProfile(
            entity=_identity("asset-id:SN-77", "Primary Pump"),
            ontology_version_id=tbox.tbox_id,
            authority=AuthorityLevel.AUTHORITATIVE,
            status=GovernanceStatus.PUBLISHED,
            evidence=_profile(
                _identity("asset-id:SN-77", "Primary Pump"), suffix="sn77"
            ).evidence,
        )
        source = FakeSource((target,))

        suggestion = EntityResolutionService(
            source, active_tbox=tbox
        ).suggest(
            _principal(),
            _identity("llm-candidate:77", "Pump mentioned in report"),
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.AUTO_LINK)
        self.assertEqual(suggestion.target, target.entity)
        self.assertEqual(
            suggestion.evidence[0].match_kind,
            "EXACT_IDENTITY_PROPERTIES",
        )
        self.assertEqual(
            suggestion.matcher_version,
            "tbox-identity-properties:v1",
        )
        self.assertEqual(source.calls[1]["kind"], "exact-identity-properties")

    def test_declared_identity_mismatch_never_falls_back_to_alias_auto_link(
        self,
    ) -> None:
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="plant-assets",
            version=2,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Asset",
                    ("asset-id",),
                    properties=(
                        PropertyDefinition(
                            "serial_number",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                    ),
                    identity_properties=("serial_number",),
                ),
            ),
            relationship_types=(),
        )
        alias_target = _profile(
            _identity(
                "asset-id:SN-88",
                "Primary Pump",
                aliases=("Pump from report",),
            ),
            suffix="sn88",
        )
        source = FakeSource((alias_target,))

        suggestion = EntityResolutionService(source, active_tbox=tbox).suggest(
            _principal(),
            _identity("llm-candidate:77", "Pump from report"),
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.NO_MATCH)
        self.assertIsNone(suggestion.target)
        self.assertEqual(
            [call["kind"] for call in source.calls],
            ["exact-key", "exact-identity-properties"],
        )

    def test_partial_identity_contract_is_conflict_without_authority_query(self) -> None:
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="plant-assets",
            version=2,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Asset",
                    ("asset-id",),
                    properties=(
                        PropertyDefinition(
                            "serial_number",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                        PropertyDefinition(
                            "site_code",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                    ),
                    identity_properties=("serial_number", "site_code"),
                ),
            ),
            relationship_types=(),
        )
        source = FakeSource(())
        suggestion = EntityResolutionService(source, active_tbox=tbox).suggest(
            _principal(),
            _identity("llm-candidate:77", "Pump"),
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.CONFLICT)
        self.assertIn("active T-Box", suggestion.reason)
        self.assertEqual(
            [call["kind"] for call in source.calls],
            ["exact-key"],
        )

    def test_ambiguous_and_duplicate_identity_values_fail_closed(self) -> None:
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="plant-assets",
            version=2,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Asset",
                    ("asset-id",),
                    properties=(
                        PropertyDefinition(
                            "serial_number",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                    ),
                    identity_properties=("serial_number",),
                ),
            ),
            relationship_types=(),
        )
        profiles = tuple(
            AuthoritativeEntityProfile(
                entity=_identity(key, name),
                ontology_version_id=tbox.tbox_id,
                authority=AuthorityLevel.AUTHORITATIVE,
                status=GovernanceStatus.PUBLISHED,
                evidence=_profile(_identity(key, name), suffix=name).evidence,
            )
            for key, name in (
                ("asset-id:A-SN-77", "Pump A"),
                ("asset-id:B-SN-77", "Pump B"),
            )
        )
        ambiguous = EntityResolutionService(
            FakeSource(profiles), active_tbox=tbox
        ).suggest(
            _principal(),
            _identity("llm-candidate:77", "Pump"),
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )[0]
        self.assertEqual(ambiguous.outcome, ResolutionOutcome.CONFLICT)
        self.assertIsNone(ambiguous.target)

        source = FakeSource(())
        duplicate = EntityResolutionService(source, active_tbox=tbox).suggest(
            _principal(),
            _identity("llm-candidate:77", "Pump"),
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )[0]
        self.assertEqual(duplicate.outcome, ResolutionOutcome.CONFLICT)
        self.assertIn("duplicated", duplicate.reason)
        self.assertEqual([call["kind"] for call in source.calls], ["exact-key"])

    def test_unique_exact_canonical_key_is_the_strongest_auto_link(self) -> None:
        target = _profile(_identity("asset-id:P-7", "Primary Pump"), suffix="p7")
        source = FakeSource((target,))
        candidate = _identity("asset-id:P-7", "Pump 7 candidate")

        suggestions = EntityResolutionService(
            source, active_tbox=_tbox()
        ).suggest(_principal(), candidate)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].outcome, ResolutionOutcome.AUTO_LINK)
        self.assertEqual(suggestions[0].target, target.entity)
        self.assertEqual(suggestions[0].confidence, 1.0)
        self.assertEqual(suggestions[0].evidence[0].match_kind, "EXACT_CANONICAL_KEY")
        self.assertEqual(suggestions[0].rule_version, "authoritative-resolution-rules:v1")
        self.assertEqual(source.calls[0]["ontology_version_id"], _tbox().tbox_id)

    def test_one_unique_governed_alias_can_auto_link(self) -> None:
        target = _profile(
            _identity("asset-id:P-7", "Primary Pump Seven", aliases=("Pump 7",)),
            suffix="p7",
        )
        candidate = _identity("llm-candidate:1", "pump-7")
        suggestions = EntityResolutionService(
            FakeSource((target,)), active_tbox=_tbox()
        ).suggest(_principal(), candidate)

        self.assertEqual(suggestions[0].outcome, ResolutionOutcome.AUTO_LINK)
        self.assertEqual(suggestions[0].evidence[0].match_kind, "EXACT_GOVERNED_ALIAS")
        self.assertEqual(suggestions[0].evidence[0].authoritative_evidence, target.evidence)

    def test_same_canonical_name_never_auto_merges_homonyms(self) -> None:
        first = _profile(_identity("asset-id:SEAL-A", "Seal"), suffix="seal-a")
        second = _profile(_identity("asset-id:SEAL-B", "Seal"), suffix="seal-b")
        candidate = _identity("llm-candidate:seal", "Seal")

        suggestions = EntityResolutionService(
            FakeSource((first, second)), active_tbox=_tbox()
        ).suggest(_principal(), candidate)

        self.assertEqual(len(suggestions), 2)
        self.assertEqual(
            {item.outcome for item in suggestions}, {ResolutionOutcome.CONFLICT}
        )
        self.assertNotIn(ResolutionOutcome.AUTO_LINK, {item.outcome for item in suggestions})

    def test_ambiguous_governed_alias_is_a_conflict(self) -> None:
        first = _profile(
            _identity("asset-id:P-7-A", "Feed Pump", aliases=("Pump Seven",)),
            suffix="pump-a",
        )
        second = _profile(
            _identity("asset-id:P-7-B", "Cooling Pump", aliases=("Pump Seven",)),
            suffix="pump-b",
        )
        candidate = _identity("llm-candidate:pump", "Pump Seven")

        suggestions = EntityResolutionService(
            FakeSource((first, second)), active_tbox=_tbox()
        ).suggest(_principal(), candidate)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].outcome, ResolutionOutcome.CONFLICT)
        self.assertIsNone(suggestions[0].target)
        self.assertIn("multiple active authoritative entities", suggestions[0].reason)

    def test_alias_uniqueness_is_global_not_limited_to_fuzzy_sample(self) -> None:
        visible = _profile(
            _identity("asset-id:P-7-A", "Feed Pump", aliases=("Pump Seven",)),
            suffix="pump-a",
        )
        outside_fuzzy_limit = _profile(
            _identity("asset-id:P-7-B", "Cooling Pump", aliases=("Pump Seven",)),
            suffix="pump-b",
        )
        source = FakeSource((visible, outside_fuzzy_limit))
        candidate = _identity("llm-candidate:pump", "Pump Seven")
        service = EntityResolutionService(
            source,
            active_tbox=_tbox(),
            policy=ResolutionPolicy(authority_candidate_limit=1),
        )

        suggestion = service.suggest(_principal(), candidate)[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.CONFLICT)
        self.assertIsNone(suggestion.target)
        self.assertNotIn("fuzzy", [call["kind"] for call in source.calls])

    def test_name_similarity_is_review_only_and_auditable(self) -> None:
        target = _profile(
            _identity("asset-id:C-101", "Compressor A-101"), suffix="compressor"
        )
        candidate = _identity("llm-candidate:compressor", "Compressor A101")
        suggestion = EntityResolutionService(
            FakeSource((target,)), active_tbox=_tbox()
        ).suggest(_principal(), candidate)[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.REVIEW)
        self.assertEqual(suggestion.evidence[0].match_kind, "SIMILAR_NAME")
        self.assertEqual(suggestion.matcher_version, "sequence-matcher:v1")
        self.assertGreaterEqual(suggestion.confidence, 0.84)

    def test_cross_tenant_candidate_is_blocked_without_querying_authority(self) -> None:
        source = FakeSource(())
        candidate = _identity(
            "llm-candidate:foreign",
            "Foreign Pump",
            tenant_id="tenant-other",
        )
        suggestion = EntityResolutionService(
            source, active_tbox=_tbox()
        ).suggest(_principal(), candidate)[0]

        self.assertEqual(suggestion.outcome, ResolutionOutcome.CONFLICT)
        self.assertIsNone(suggestion.target)
        self.assertEqual(source.calls, [])

    def test_source_boundary_violation_fails_closed(self) -> None:
        foreign = _profile(
            _identity(
                "asset-id:FOREIGN",
                "Foreign Pump",
                tenant_id="tenant-other",
            ),
            suffix="foreign",
        )
        candidate = _identity("llm-candidate:foreign", "Foreign Pump")
        with self.assertRaises(ResolutionBoundaryError):
            EntityResolutionService(
                FakeSource((foreign,)), active_tbox=_tbox()
            ).suggest(_principal(), candidate)


class _Rows:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)

    def single(self):  # type: ignore[no-untyped-def]
        if not self.rows:
            return None
        if len(self.rows) != 1:
            raise AssertionError("single() received multiple fake rows")
        return self.rows[0]


class _Session:
    def __init__(
        self,
        rows: tuple[dict[str, object], ...],
        *,
        subsequent_rows: tuple[tuple[dict[str, object], ...], ...] = (),
    ) -> None:
        self.results = [rows, *subsequent_rows]
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> _Rows:
        self.calls.append((query, parameters))
        index = len(self.calls) - 1
        if index >= len(self.results):
            raise AssertionError("fake session has no rows for this query")
        return _Rows(self.results[index])

    def execute_read(self, callback, *args):  # type: ignore[no-untyped-def]
        return callback(self, *args)


class _Driver:
    def __init__(self, session: _Session) -> None:
        self.test_session = session
        self.databases: list[str] = []

    def session(self, *, database: str) -> _Session:
        self.databases.append(database)
        return self.test_session


class Neo4jAuthoritativeEntitySourceTests(unittest.TestCase):
    def test_identity_property_query_is_publication_tbox_and_acl_bounded(self) -> None:
        identity = _identity("asset-id:SN-77", "Primary Pump")
        target = {
            "entity_id": identity.entity_id,
            "tenant_id": identity.tenant_id,
            "entity_type": identity.entity_type,
            "canonical_key": identity.canonical_key,
            "canonical_name": identity.canonical_name,
            "aliases": [],
            "ontology_version_id": _tbox().tbox_id,
            "matched_value": "serial_number=STRING:SN-77",
            "evidence": [
                {
                    "mention_revision_id": "mention-sn77",
                    "document_id": "document-sn77",
                    "version_id": "version-sn77",
                    "chunk_id": "chunk-sn77",
                    "char_start": 0,
                    "char_end": 5,
                    "quoted_text": "SN-77",
                }
            ],
        }
        session = _Session(
            (
                {
                    "match_count": 1,
                    "only_entity_id": identity.entity_id,
                    "publication_id": "publication-1",
                    "activation_generation": 2,
                    "publication_count": 1,
                },
            ),
            subsequent_rows=(({"target": target},),),
        )
        source = Neo4jAuthoritativeEntitySource(_Driver(session))

        result = source.find_exact_identity_properties(
            _principal(),
            ontology_version_id=_tbox().tbox_id,
            entity_type="Asset",
            identity_properties=(
                IdentityPropertyValue("serial_number", "STRING", "SN-77"),
            ),
        )

        self.assertEqual(result.target.entity, identity)  # type: ignore[union-attr]
        query, parameters = session.calls[0]
        for required in (
            "all(",
            "PUBLISHES_KNOWLEDGE_REVISION",
            "fact:GovernedAssertionRevision",
            "fact.literal_canonical_value = identity.canonical_value",
            "fact.ontology_version_id = $ontology_version_id",
            "any(group IN $groups WHERE group IN fact.access_groups)",
            "any(group IN $groups WHERE group IN fact_chunk.access_groups)",
            "any(group IN $groups WHERE group IN fact_document.access_groups)",
        ):
            self.assertIn(required, query)
        self.assertEqual(
            parameters["identity_properties"],
            [
                {
                    "name": "serial_number",
                    "datatype": "STRING",
                    "canonical_value": "SN-77",
                    "canonical_unit": None,
                }
            ],
        )
        self.assertEqual(len(session.calls), 2)
        fetch_query, fetch_parameters = session.calls[1]
        self.assertEqual(fetch_parameters["identity_properties"], parameters["identity_properties"])
        self.assertEqual(fetch_parameters["publication_id"], "publication-1")
        self.assertEqual(fetch_parameters["activation_generation"], 2)
        self.assertEqual(fetch_parameters["only_entity_id"], identity.entity_id)
        self.assertIn("state.activation_generation = $activation_generation", fetch_query)
        self.assertIn("fact.literal_canonical_value = identity.canonical_value", fetch_query)

    def test_identity_property_conflict_counts_every_match_without_fetching_target(self) -> None:
        session = _Session(({
            "match_count": 2,
            "only_entity_id": "not-a-unique-target",
            "publication_id": "publication-1",
            "activation_generation": 2,
            "publication_count": 1,
        },))
        source = Neo4jAuthoritativeEntitySource(_Driver(session))
        result = source.find_exact_identity_properties(
            _principal(),
            ontology_version_id=_tbox().tbox_id,
            entity_type="Asset",
            identity_properties=(IdentityPropertyValue("serial_number", "STRING", "SN-77"),),
        )
        self.assertEqual(result.match_count, 2)
        self.assertIsNone(result.target)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("LIMIT", session.calls[0][0])

    def test_query_and_mapping_are_tenant_acl_tbox_and_authority_bounded(self) -> None:
        identity = _identity("asset-id:P-7", "Primary Pump", aliases=("Pump 7",))
        row = {
            "entity_id": identity.entity_id,
            "tenant_id": identity.tenant_id,
            "entity_type": identity.entity_type,
            "canonical_key": identity.canonical_key,
            "canonical_name": identity.canonical_name,
            "aliases": list(identity.aliases),
            "ontology_version_id": _tbox().tbox_id,
            "evidence": [
                {
                    "mention_revision_id": "mention-p7",
                    "document_id": "document-p7",
                    "version_id": "version-p7",
                    "chunk_id": "chunk-p7",
                    "char_start": 10,
                    "char_end": 16,
                    "quoted_text": "Pump 7",
                }
            ],
        }
        session = _Session((row,))
        source = Neo4jAuthoritativeEntitySource(_Driver(session))
        result = source.list_authoritative_entities(
            _principal(),
            ontology_version_id=_tbox().tbox_id,
            entity_type="Asset",
            limit=25,
        )

        self.assertEqual(result[0].entity, identity)
        self.assertEqual(result[0].authority, AuthorityLevel.AUTHORITATIVE)
        query, parameters = session.calls[0]
        for required in (
            "ACTIVE_KNOWLEDGE_PUBLICATION",
            "PUBLISHES_KNOWLEDGE_REVISION",
            "ACTIVE_SNAPSHOT",
            "ACTIVE_VERSION",
            "USES_KNOWLEDGE_SNAPSHOT",
            "tbox:TBoxVersion",
            "status: 'PUBLISHED'",
            "mention:GovernedEntityMentionRevision",
            "authority_level: 'AUTHORITATIVE'",
            "governance_status: 'PUBLISHED'",
            "entity:Entity",
            "any(group IN $groups WHERE group IN mention.access_groups)",
            "any(group IN $groups WHERE group IN chunk.access_groups)",
            "any(group IN $groups WHERE group IN document.access_groups)",
            "mention.evidence_char_start < mention.evidence_char_end",
            "substring(",
            ") = mention.evidence_text",
        ):
            self.assertIn(required, query)
        self.assertEqual(parameters["tenant_id"], TENANT)
        self.assertEqual(parameters["groups"], ["asset-engineers"])
        self.assertEqual(parameters["ontology_version_id"], _tbox().tbox_id)

    def test_exact_key_query_counts_globally_before_returning_unique_target(self) -> None:
        identity = _identity("asset-id:P-7", "Primary Pump", aliases=("Pump 7",))
        target = {
            "entity_id": identity.entity_id,
            "tenant_id": identity.tenant_id,
            "entity_type": identity.entity_type,
            "canonical_key": identity.canonical_key,
            "canonical_name": identity.canonical_name,
            "aliases": list(identity.aliases),
            "ontology_version_id": _tbox().tbox_id,
            "matched_value": identity.canonical_key,
            "evidence": [
                {
                    "mention_revision_id": "mention-p7",
                    "document_id": "document-p7",
                    "version_id": "version-p7",
                    "chunk_id": "chunk-p7",
                    "char_start": 10,
                    "char_end": 16,
                    "quoted_text": "Pump 7",
                }
            ],
        }
        session = _Session(
            (
                {
                    "match_count": 1,
                    "only_entity_id": identity.entity_id,
                    "publication_id": "publication-1",
                    "activation_generation": 1,
                    "publication_count": 1,
                },
            ),
            subsequent_rows=(({"target": target},),),
        )
        source = Neo4jAuthoritativeEntitySource(_Driver(session))

        result = source.find_exact_canonical_key(
            _principal(),
            ontology_version_id=_tbox().tbox_id,
            entity_type="Asset",
            canonical_key=identity.canonical_key,
        )

        self.assertEqual(result.match_count, 1)
        self.assertEqual(result.target.entity, identity)  # type: ignore[union-attr]
        query, parameters = session.calls[0]
        self.assertIn("count(DISTINCT entity) AS match_count", query)
        self.assertIn("entity.canonical_key = $canonical_key", query)
        self.assertIn("ACTIVE_KNOWLEDGE_PUBLICATION", query)
        self.assertIn("ACTIVE_SNAPSHOT", query)
        self.assertIn(") = mention.evidence_text", query)
        self.assertEqual(parameters["canonical_key"], identity.canonical_key)
        target_query, target_parameters = session.calls[1]
        self.assertIn(
            "state.activation_generation = $activation_generation",
            target_query,
        )
        self.assertIn("publication.publication_id = $publication_id", target_query)
        self.assertIn("entity.entity_id = $only_entity_id", target_query)
        self.assertEqual(target_parameters["publication_id"], "publication-1")
        self.assertEqual(target_parameters["activation_generation"], 1)
        self.assertEqual(target_parameters["only_entity_id"], identity.entity_id)

    def test_exact_alias_ambiguous_count_never_selects_bounded_target(self) -> None:
        session = _Session(
            (
                {
                    "match_count": 2,
                    "only_entity_id": "ignored",
                    "publication_id": "publication-1",
                    "activation_generation": 1,
                    "publication_count": 1,
                },
            )
        )
        source = Neo4jAuthoritativeEntitySource(_Driver(session))

        result = source.find_exact_governed_alias(
            _principal(),
            ontology_version_id=_tbox().tbox_id,
            entity_type="Asset",
            candidate_values=("pump-7",),
        )

        self.assertEqual(result, ExactAuthoritativeMatch(2))
        query, parameters = session.calls[0]
        self.assertIn("count(DISTINCT entity) AS match_count", query)
        self.assertIn("normalize(alias, NFKC) =~ pattern", query)
        self.assertIn("ACTIVE_KNOWLEDGE_PUBLICATION", query)
        self.assertEqual(len(parameters["alias_patterns"]), 1)

    def test_exact_target_fetch_fails_closed_if_active_boundary_changes(self) -> None:
        identity = _identity("asset-id:P-7", "Primary Pump")
        session = _Session(
            (
                {
                    "match_count": 1,
                    "only_entity_id": identity.entity_id,
                    "publication_id": "publication-1",
                    "activation_generation": 3,
                    "publication_count": 1,
                },
            ),
            subsequent_rows=((),),
        )
        source = Neo4jAuthoritativeEntitySource(_Driver(session))

        with self.assertRaisesRegex(
            ResolutionBoundaryError,
            "active authority changed",
        ):
            source.find_exact_canonical_key(
                _principal(),
                ontology_version_id=_tbox().tbox_id,
                entity_type="Asset",
                canonical_key=identity.canonical_key,
            )

        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
