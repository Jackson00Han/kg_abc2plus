# Industrial electrical knowledge model

This model supports evidence discovery around Canalis KT busbar trunking and
EvoPacT HVX up-to-24-kV vacuum circuit breakers. Its first job is to preserve
identity, applicable context and evidence; a useful diagram follows from those
semantics. The domain is composed into one tenant-owned, versioned T-Box.

## Four different kinds of structure

| View | Meaning | What it cannot establish |
| --- | --- | --- |
| Classification | An equipment class is a specialization of another class | A particular installed asset has a particular nameplate rating |
| Physical composition | A component belongs to an asset or an asset belongs to a system | Electrical connectivity or the direction of fault propagation |
| Connection | Two identified assets are connected in a documented topology | That one asset caused another asset's observation |
| Diagnostic evidence | An observation suggests candidate explanations and relevant tests | A confirmed diagnosis without the required measurements and review |

Classification and physical composition use explicit child-to-parent relation
definitions with acyclic semantics. Their graph instances are validated against
the complete proposed publication, including relationships added in different
batches. Display depth is calculated for a selected view; it is not a universal
`layer` property or an authority ranking.

Equipment-class concepts are graph entities. This property-graph model does
not add an OWL reasoner, implicit property inheritance or unbounded transitive
inference. The governed relation types and hierarchy declarations express the
supported behavior directly.

## Identity before similarity

An equipment class, a product family, a product model and an installed asset
have different identities. Two assets may share a display name while belonging
to different systems or access groups. Demo asset identifiers identify authored
examples; they are not invented Schneider ordering codes. A model or voltage
mentioned in one document does not automatically apply to another HVX variant.

The industrial corpus uses a separate tenant and public, engineering and
maintenance access groups. Existing finance/legal fixtures remain regression
fixtures. Changing the displayed persona is never sufficient to authorize a
different tenant or to initialize its vector generation.

## Evidence and trust

Keep the following dimensions independent:

- **Source kind:** original official publication, project-curated reference,
  or synthetic field record.
- **Knowledge origin:** expert import, model extraction, rule derivation or
  another explicitly declared origin.
- **Authority:** reference authority or secondary knowledge under the existing
  governance model. Project curation is not Schneider expert certification.
- **Review state:** candidate, approved, published or another lifecycle state.
  Approval does not automatically change secondary source authority.
- **Applicability:** product variant, source revision, installed asset and
  observation time. Confidence is not a substitute for any of these fields.

The authored reference graph exercises the expert-import workflow. Synthetic
records exercise the secondary workflow with deterministic rule-derived
provenance; they must not be presented as actual LLM extraction or real field
reports. User-triggered model construction is a separate, audited workflow.

Source Chunks remain the factual evidence for retrieval and citations. PDF
evidence additionally retains the original checksum, selected physical pages,
normalizer version and exact page/table/text source map. A diagram edge is a
navigational assertion with evidence, not a replacement for that evidence.

The first industrial corpus keeps measured values, units, measuring points and
timestamps in exact source sections. Its selected graph links observations to
events and assets; it does not yet publish those measurements as typed numeric
properties. The general governed-literal machinery exists, but this industrial
T-Box/corpus does not seed it. A later numeric extension must introduce a new
immutable T-Box and corpus revision with evidence-backed literal records.

## Development sequence

First verify one Canalis case through source, identity, evidence, review and
publication. Then include the distinct HVX family and representative related
field documents. Count documents, source Chunks and published graph records
separately: the source corpus can contain hundreds of Chunks while the selected
governed graph stays within the existing 500-record publication limit.

Retrieval evaluation and the reusable graph explorer are subsequent milestones
in `industrial_development_plan.md`. Their acceptance depends on preserving
these semantics and access boundaries, rather than merely drawing every node.
