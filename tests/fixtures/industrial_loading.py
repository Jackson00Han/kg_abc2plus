"""Small authored industrial sources for isolated lifecycle checks, not scale evidence."""

from datetime import UTC, datetime

from graphrag_prod.industrial.corpus import (
    CorpusDocument, CorpusEntity, CorpusRelationship, CorpusSection,
    IndustrialCorpus, SourceReference,
)
from graphrag_prod.industrial.loading import INDUSTRIAL_TENANT, _LoaderLease
from graphrag_prod.ingestion.pipeline import EmbeddingProfile


NOW = datetime(2026, 9, 6, 1, tzinfo=UTC)
PROFILE = EmbeddingProfile("fixture", "industrial-isolated-fixture", "v1", 4, "unit")


def tiny_corpus() -> IndustrialCorpus:
    curated = "SME_REVIEW_PENDING\nProjectBusbar is a subtype of ProjectElectrical.\n"
    field = "SYNTHETIC_FIELD_RECORD 合成\nProjectAsset is installed at ProjectSite.\n"
    docs = (
        CorpusDocument("reference-types", "Project classification reference", "CURATED_REFERENCE", "canalis-kt",
            ("engineering",), NOW.isoformat(), curated,
            (CorpusSection("classification", "Classification", 0, len(curated)),),
            (SourceReference("canalis-kt-installation", (71,), "QGH3492101-04"),)),
        CorpusDocument("field-assets", "Project site record", "SYNTHETIC_FIELD_RECORD", "canalis-kt",
            ("maintenance",), NOW.isoformat(), field,
            (CorpusSection("installation", "Installation", 0, len(field)),), ()),
    )
    entities = (
        CorpusEntity("busbar-class", "EquipmentClass", "ProjectBusbar", "industrial:busbar-class", "reference-types", "classification"),
        CorpusEntity("electrical-class", "EquipmentClass", "ProjectElectrical", "industrial:electrical-class", "reference-types", "classification"),
        CorpusEntity("project-asset", "InstalledAsset", "ProjectAsset", "industrial:project-asset", "field-assets", "installation"),
        CorpusEntity("project-site", "Site", "ProjectSite", "industrial:project-site", "field-assets", "installation"),
    )
    relationships = (
        CorpusRelationship("busbar-subtype", "busbar-class", "SUBTYPE_OF", "electrical-class", "reference-types", "classification"),
        CorpusRelationship("asset-site", "project-asset", "INSTALLED_AT", "project-site", "field-assets", "installation"),
    )
    return IndustrialCorpus("industrial-small-test-v1", INDUSTRIAL_TENANT, docs, entities, relationships, "test-only-small-fixture")


class FixtureEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return (1.0, 0.0, 0.0, 0.0)


def load_small_fixture(loader, embedder, *, artifacts=(), catalog=None, original_cache=None):
    """Exercise the same internals; public loader still requires full scale preflight."""
    with _LoaderLease(loader.driver, loader.database, INDUSTRIAL_TENANT) as lease:
        return loader._run(tiny_corpus(), PROFILE, embedder, normalized_artifacts=artifacts,
            catalog=catalog, original_cache=original_cache, heartbeat=lease.refresh)
