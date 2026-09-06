# Representative industrial corpus

`datasets/industrial-v1/corpus` contains 34 authored documents and 330 semantic
sections: two curated references and four related records for each of eight
synthetic installed assets. Both Canalis KT and EvoPacT HVX up to 24 kV have
four assets, across two synthetic facilities and four electrical systems.

The four per-asset documents are an engineering register, maintenance
observation, maintenance follow-up, and engineering handover. Their ten
sections each carry substantive context. They preserve identities, measurements with units,
conditions, dates, conflicts, missing evidence and the limits of conclusions.
The builder rejects short or repeated sections used to inflate scale; a content
regression also limits recurring long-sentence boilerplate. Logical attachments
are explicitly synthetic inline excerpts, not claims that separate original
photos or CSV files have been provided.

## Eight related scenarios

| Asset | Scenario and evidence boundary |
| --- | --- |
| BKT-A01 | Surface thermal observations with different load/environment/instrument conditions; no universal temperature threshold |
| BKT-A02 | Nearby water marks and reflections; no unsupported claim of internal water ingress |
| BKT-B01 | Local color change and a wrongly assigned cross-site image; shared alias does not merge identities |
| BKT-B02 | A confirmed synthetic report mapping error: TT-07 belongs to the adjacent test cabinet, TT-09 to the busbar; the corrected 14 April history and 18 April samples retain separate timestamps |
| HVX-A01 | Recovered historical controller logs explain one remote request rejected in LOCAL mode; no claim of coil health or a new successful remote operation |
| HVX-A02 | Image versus background charging-state contradiction; a single zero-current sample is not an action waveform |
| HVX-B01 | Trip event with incomplete command and clock evidence; no inferred internal interrupter damage |
| HVX-B02 | Racking-position disagreement and a missing export interval; no inferred interlock failure |

HVX China 12 kV, HVX-O, SureSeT 5/15 kV and Canalis KR appear as explicitly
rejected or pending source attachments. They do not introduce their technical
parameters into the two core families. The family-to-contract mapping is
explicit in both the index and the manifest.

## Identity and provenance

`build_corpus()` returns immutable documents, exact sections, entity seeds,
relationship seeds and a semantic manifest checksum. Section offsets cover
every authored character exactly once, including Markdown headings and final
newlines. A section is a bounded source chunk, not a generated answer. Changes
to source text, metadata, identities or evidence bindings change the checksum.

The selected graph contains an explicit project classification hierarchy,
physical composition, separate electrical connection edges for two synthetic
busbar segments and their joint, family/configuration/instance separation, and event-scoped
observations. An observation belongs to an inspection event and describes a
symptom; it is not stored as a timeless diagnosis on the asset. Official
troubleshooting examples retain possible-cause semantics and a check-related
navigation edge. Every relationship's evidence section contains both endpoint
names, permitting exact endpoint mentions in the existing governed loader.

The graph uses only a selected subset of source sections. Its conservative
record estimate includes endpoint mentions and stays under the existing
500-record publication ceiling. Corpus chunk count is independent of that
graph record count.

## Trust and access

The `public` group receives two AI-assisted project-curated references, each retaining
official edition/page pointers and `SME_REVIEW_PENDING`. These are explicitly
project baselines pending domain expert review, not Schneider certification or
human-adjudicated expert knowledge. Field records are always
synthetic secondary knowledge. `engineering` receives registers and summaries;
`maintenance` receives original observation and follow-up fixtures. Internal
maintenance markers never appear in the engineering summaries or public
references.

Two synthetic cases reach bounded conclusions supported by the inline record
rows: BKT-B02's channel mapping correction and HVX-A01's historical mode
rejection. Other cases retain different unresolved gaps. None establishes a
real equipment diagnosis. Numerical records are authored samples, not
manufacturer ratings or thresholds. The corpus supports testing
evidence capture, applicability, uncertainty and authority distinctions; it
cannot establish real-world diagnosis accuracy without approved field cases.

`family` and `asset_keys` explicitly declare each document's primary scope.
Every field document names one installed-asset entity key, such as
`asset-bkt-a01`; the public references have no installed-asset scope. Mentioning
another asset for comparison does not add it to the primary filter. These
fields are validated and included in the immutable manifest checksum.

General record-handling rules belong here: preserve original versions, label
sample times independently from publication times, keep absent values absent,
and compare the same physical region and acquisition conditions before
interpreting changes. A corrected report field does not itself certify device
health. The field documents carry the particular rows, conditions, revisions,
and open tasks needed to apply these rules to each case.

## Repeatable validation

```sh
uv run python scripts/build_industrial_corpus.py --check
uv run python scripts/validate_industrial_model.py
uv run python -m unittest tests.unit.test_industrial_corpus -v
```

The builder operates only on committed authored source files. Public manual
acquisition and PDF normalization are separate, checksum-pinned workflows.
Gold retrieval questions and model predictions are not generated by this
builder and will be evaluated independently in the retrieval milestone.
