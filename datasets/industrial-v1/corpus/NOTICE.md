# Industrial corpus v1

These authored Markdown files are project development fixtures. All installed
assets, facilities, configurations, observations, instruments, measurements,
operators and field events are synthetic. They are not Schneider Electric
customer records, approved technical limits, certified expert conclusions or
instructions for operating electrical equipment.

The two `CURATED_REFERENCE` documents contain AI-assisted project paraphrases
and bibliographic pointers to inspected public manuals. They explicitly retain
`SME_REVIEW_PENDING`. Their project taxonomy is a modeling convention, not a
claim about a manufacturer's taxonomy. Official manuals remain outside Git.

`SYNTHETIC_FIELD_RECORD` documents preserve conflicting reports and distinct
unresolved causes. Two bounded synthetic cases close a report mapping error
and a historical request rejected by project mode logic; these are not real
field diagnoses. These records must not become authoritative manufacturer facts
through ingestion, review, visualization, or answer generation.

`index.json` declares identities, explicit product/primary-asset scope, access
groups, bibliographic anchors and the
selected graph. `manifest.json` is rebuilt from the committed Markdown by:

```sh
uv run python scripts/build_industrial_corpus.py --check
```

No network, embedding provider, extraction model or answer predictions are
needed. Review intended authored changes before running the same command with
`--write`. This corpus is development evidence, not production qualification.
