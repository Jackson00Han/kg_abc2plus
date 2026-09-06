# Industrial electrical knowledge ontology

`build_industrial_tbox(tenant_id)` in
`graphrag_prod.industrial.ontology` returns a draft of the composed
`industrial-electric-v1` T-Box. Canalis and EvoPacT examples share this
versioned schema, allowing one tenant publication to bind one exact T-Box.
It is a project application schema, not an official Schneider Electric
ontology or approved diagnostic guidance.

## Independent semantics

| Meaning | Entity/relationship contract |
| --- | --- |
| Classification | `EquipmentClass -SUBTYPE_OF-> EquipmentClass`, specific concept to general concept |
| Product identity | `ProductModel -IN_FAMILY-> ProductFamily` and `ProductModel -CLASSIFIED_AS-> EquipmentClass` |
| Installed identity | `InstalledAsset -INSTANCE_OF-> ProductModel`, with site-scoped stable canonical keys |
| Composition | `InstalledAsset/Component -PART_OF-> IndustrialSystem/InstalledAsset/Component`, child to parent |
| Location | `InstalledAsset -INSTALLED_AT-> Site`; `IndustrialSystem -LOCATED_AT-> Site` |
| Electrical connection | `CONNECTS_TO`, independent of composition and its acyclicity rule |
| Observations | `InspectionEvent`, `Observation`, `HAS_SYMPTOM`, `OBSERVED_ON`, `OBSERVES`, `DESCRIBES` |
| Diagnostic possibility | `Symptom -MAY_INDICATE-> FaultMode/DiagnosticCondition`; `CHECKED_BY` and `ADDRESSED_BY` retain their source conditions |
| Source applicability | `SourceEdition -APPLIES_TO-> ProductFamily/ProductModel`, only with explicit source evidence |

The schema distinguishes a manufacturer catalogue model from an explicitly
labelled project configuration via an optional `ModelKind` property.
Simulated equipment and configurations cannot become manufacturer catalogue
facts merely by sharing a product-family name. A canonical key uses
`industrial:<stable-domain-key>`. Shared aliases do not identify the same
asset. Automatically extracted candidates retain `llm-candidate` identities
until the existing evidence-backed resolution workflow confirms identity.

`HAS_SYMPTOM` records a sourced observation association, not a timeless
current condition. `MAY_INDICATE` expresses a possible cause and never asserts
that a diagnosis is confirmed. Structured observation/event times are
optional, evidence-backed properties. Missing time or applicability cannot be
invented by graph traversal.

`DiagnosticCondition` also covers possible normal blocking states, such as an
uncharged operating mechanism or a mechanical interlock restricting closing.
These conditions do not by themselves establish damage or malfunction.
`FaultMode` remains available for explicitly described candidate failures;
neither type alone confirms a failure of the installed asset.

## Explicit hierarchy declaration

T-Boxes may declare up to 32 optional `hierarchies`. Each declaration includes
`name`, `relationship_type`, `kind` (`CLASSIFICATION` or `COMPOSITION`),
`node_types`, `acyclic: true`, and optional `description`. Relations point from
child to parent. The relation must already be declared by the T-Box; hierarchy
node types must exactly cover its endpoint type sets. Classification uses one
concept node type. Duplicate hierarchy names or relation bindings are rejected.

The industrial composition relation permits at most one parent per object;
the existing complete-publication endpoint-cardinality check enforces this.
Classification may have multiple broader concepts if the result remains a
directed acyclic graph. No numeric `layer` field is required. Presentation
ordering may later use a separate visualization profile.

These declarations do **not** implement OWL subclass entailment, inherited
properties, inferred transitive edges, causal inference or automatic technical
rule approval. They express explicit navigation semantics and validation
invariants over existing, sourced edges.

## Publication boundary and compatibility

Fresh publication, publication replay and rollback load the exact
checksum-bound T-Box within the existing publication transaction, then
validate the complete final manifest. Missing or corrupted T-Box definitions
fail closed. The final graph includes carried revisions and additions after
removals/replacements. A cyclic final manifest is rejected and transaction
rollback preserves the previous publication and approved record heads.

The hierarchy validator deduplicates repeated evidence for a canonical edge,
validates endpoint and identity types, and uses iterative Kahn topological
sorting. It rejects self-loops, cycles and excessive work. Validation is
bounded to 5,000 incoming edges and 5,000 hierarchy nodes; current publication
manifests remain independently limited to 500 governed records. The validator
must not be applied to a partial ACL-filtered retrieval graph as a substitute
for complete-manifest validation.

Hierarchy declarations are persisted inside existing immutable T-Box
`definition_json`; no database schema migration is required. T-Boxes with no
hierarchies omit the extension from serialized and canonical definitions.
Their previous semantic checksums remain unchanged. Adding a non-empty
hierarchy contract changes the checksum and requires the normal draft/version
and publication workflow.

## Repeatable checks

```sh
.venv/bin/python -m unittest tests.unit.test_industrial_ontology \
  tests.unit.test_ontology tests.unit.test_knowledge_review -q
```

Using the project's explicitly disposable Neo4j test environment, run
`tests.integration.test_ontology_neo4j` and
`tests.integration.test_knowledge_review_neo4j`. They include immutable
industrial hierarchy round trips and atomic rejection of a deliberately
malformed cyclic publication followed by successful publication of its valid
subset. The malformed fixture checks structural rejection; it does not claim
the deliberately reversed relation is supported by industrial source text.
