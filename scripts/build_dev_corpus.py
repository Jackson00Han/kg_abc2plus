#!/usr/bin/env python3
"""Build or verify the deterministic Stage 5A development corpus.

The corpus is intentionally synthetic.  It exercises provenance, temporal,
authorization, homonym, graph-navigation, and retrieval boundaries without
requiring network access or model-provider credentials.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from graphrag_prod.domain import (
    assertion_id,
    canonicalize_uri,
    chunk_id,
    content_checksum,
    document_id,
    embedding_space_id,
    entity_id,
    mention_id,
    pipeline_profile_id,
    version_id,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_DIR = ROOT / "datasets" / "dev-corpus-v1"
DATASET_ID = "dev-corpus-v1"
DATASET_VERSION = "1.0.1"
GENERATOR_VERSION = "1.1.0"
FIXED_INGESTED_AT = "2026-09-01T00:00:00+00:00"
SPLITTER_SIGNATURE = "synthetic-section-splitter:v1"
EXTRACTOR_SIGNATURE = "synthetic-adjudicated-extractor:v1"
SCHEMA_SIGNATURE = "company-filings:v1"
EMBEDDING_DIMENSIONS = 128
EMBEDDING_PROVIDER = "fixture"
EMBEDDING_MODEL = "adjudicated-evidence-clusters"
EMBEDDING_REVISION = "dev-corpus-v1.1"
EMBEDDING_NORMALIZATION = "l2"


@dataclass(frozen=True, slots=True)
class YearMetrics:
    revenue_billions: float
    gross_margin_percent: float
    cash_billions: float
    capital_return_billions: float


@dataclass(frozen=True, slots=True)
class CompanySpec:
    key: str
    ticker: str
    canonical_name: str
    surface: str
    tenant_id: str
    access_group: str
    product: str
    segment: str
    metrics: tuple[tuple[int, YearMetrics], ...]

    def metrics_for(self, year: int) -> YearMetrics:
        return dict(self.metrics)[year]


@dataclass(frozen=True, slots=True)
class SectionSpec:
    key: str
    title: str
    text: str
    predicate: str | None = None
    object_entity_key: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetBuild:
    files: dict[str, bytes]
    manifest: dict[str, Any]
    documents: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]
    questions: tuple[dict[str, Any], ...]
    vectors: tuple[dict[str, Any], ...]


COMPANIES = (
    CompanySpec(
        "northstar",
        "NST",
        "Northstar Systems plc",
        "Northstar",
        "tenant-alpha",
        "alpha-public",
        "Orbit workstation platform",
        "Enterprise Platforms",
        (
            (2023, YearMetrics(68.4, 41.8, 8.7, 5.2)),
            (2024, YearMetrics(72.1, 43.2, 9.4, 5.8)),
        ),
    ),
    CompanySpec(
        "atlas-cloud",
        "ATC",
        "Atlas Cloud Services Ltd.",
        "Atlas",
        "tenant-alpha",
        "alpha-finance",
        "Nimbus cloud platform",
        "Cloud Infrastructure",
        (
            (2023, YearMetrics(44.2, 61.0, 11.2, 2.6)),
            (2024, YearMetrics(52.8, 63.5, 14.6, 3.1)),
        ),
    ),
    CompanySpec(
        "atlas-logistics",
        "ATL",
        "Atlas Logistics Holdings Inc.",
        "Atlas",
        "tenant-alpha",
        "alpha-legal",
        "Atlas Fleet network",
        "Integrated Freight",
        (
            (2023, YearMetrics(19.7, 18.6, 2.1, 0.8)),
            (2024, YearMetrics(21.4, 19.3, 2.4, 0.9)),
        ),
    ),
    CompanySpec(
        "meridian-retail",
        "MRT",
        "Meridian Retail Group Corp.",
        "Meridian",
        "tenant-beta",
        "beta-public",
        "Compass commerce service",
        "Omnichannel Retail",
        (
            (2023, YearMetrics(33.5, 29.4, 3.3, 1.4)),
            (2024, YearMetrics(35.1, 30.2, 3.0, 1.6)),
        ),
    ),
    CompanySpec(
        "harbor-energy",
        "HBE",
        "Harbor Energy Networks S.A.",
        "Harbor",
        "tenant-beta",
        "beta-board",
        "TideGrid energy network",
        "Grid Operations",
        (
            (2023, YearMetrics(80.3, 22.7, 6.8, 4.7)),
            (2024, YearMetrics(74.9, 20.8, 5.9, 4.0)),
        ),
    ),
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: Iterable[dict[str, Any]]) -> bytes:
    return (
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
            for value in values
        )
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _feature_vector(
    feature_weights: dict[str, float],
    feature_indices: dict[str, int],
) -> tuple[float, ...]:
    """Encode adjudicated evidence concepts into an auditable fixture vector."""
    values = [0.0] * EMBEDDING_DIMENSIONS
    if not feature_weights:
        raise ValueError("fixture vector requires an adjudicated semantic feature")
    for feature, weight in feature_weights.items():
        if feature not in feature_indices:
            raise ValueError(f"unknown fixture semantic feature: {feature}")
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("fixture semantic feature weights must be positive")
        values[feature_indices[feature]] = float(weight)
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm == 0.0:
        raise ValueError("fixture vector has a zero norm")
    return tuple(round(value / norm, 8) for value in values)


def _entity_key(tenant_id: str, entity_type: str, canonical_key: str) -> str:
    return f"{tenant_id}:{entity_type.casefold()}:{canonical_key}"


def _entity_record(
    *,
    tenant_id: str,
    entity_type: str,
    canonical_key: str,
    canonical_name: str,
    aliases: Iterable[str] = (),
) -> dict[str, Any]:
    logical_key = _entity_key(tenant_id, entity_type, canonical_key)
    return {
        "aliases": sorted(set(aliases)),
        "canonical_key": canonical_key,
        "canonical_name": canonical_name,
        "entity_id": entity_id(tenant_id, entity_type, canonical_key),
        "entity_key": logical_key,
        "entity_type": entity_type,
        "tenant_id": tenant_id,
    }


def _company_entity(company: CompanySpec) -> dict[str, Any]:
    aliases = () if company.surface == company.canonical_name else (company.surface,)
    return _entity_record(
        tenant_id=company.tenant_id,
        entity_type="Company",
        canonical_key=f"ticker:{company.ticker}",
        canonical_name=company.canonical_name,
        aliases=aliases,
    )


def _product_entity(company: CompanySpec) -> dict[str, Any]:
    return _entity_record(
        tenant_id=company.tenant_id,
        entity_type="Product",
        canonical_key=f"product:{company.ticker.casefold()}-principal",
        canonical_name=company.product,
    )


def _segment_entity(company: CompanySpec) -> dict[str, Any]:
    return _entity_record(
        tenant_id=company.tenant_id,
        entity_type="BusinessSegment",
        canonical_key=f"segment:{company.ticker.casefold()}-primary",
        canonical_name=company.segment,
    )


def _risk_entity(tenant_id: str, risk_key: str, name: str) -> dict[str, Any]:
    return _entity_record(
        tenant_id=tenant_id,
        entity_type="RiskFactor",
        canonical_key=f"risk:{risk_key}",
        canonical_name=name,
    )


def _section_specs(company: CompanySpec, year: int) -> tuple[SectionSpec, ...]:
    current = company.metrics_for(year)
    if year == 2024:
        prior = company.metrics_for(2023)
    else:
        prior = YearMetrics(
            current.revenue_billions - 2.3,
            current.gross_margin_percent - 0.9,
            current.cash_billions - 0.4,
            current.capital_return_billions - 0.3,
        )
    product_key = _product_entity(company)["entity_key"]
    segment_key = _segment_entity(company)["entity_key"]
    market_key = _risk_entity(
        company.tenant_id,
        "macroeconomic-volatility",
        "Macroeconomic volatility",
    )["entity_key"]
    supply_key = _risk_entity(
        company.tenant_id,
        "supply-chain-disruption",
        "Supply-chain disruption",
    )["entity_key"]
    surface = company.surface
    return (
        SectionSpec(
            "identity",
            "Filing identity",
            "SYNTHETIC DEVELOPMENT FILING — NOT A REAL COMPANY FILING. "
            f"{surface} ({company.ticker}) identifies {company.canonical_name} as the "
            f"reporting company for fiscal year {year}. This deterministic text exists "
            "only to validate GraphRAG development behavior and is not investment data.",
        ),
        SectionSpec(
            "business",
            "Business overview",
            f"{surface} describes a synthetic business centered on its "
            f"{company.product}. Management states that this offering is sold to "
            "commercial customers under annual and multi-year arrangements, while "
            "service availability and customer support remain material obligations.",
            "OFFERS",
            product_key,
        ),
        SectionSpec(
            "product",
            "Products and services",
            f"{surface} reports that the {company.product} combines hardware, software, "
            "support, and recurring service components. The synthetic filing separates "
            "recognized revenue from contracted backlog and does not treat those values "
            "as interchangeable.",
            "OFFERS",
            product_key,
        ),
        SectionSpec(
            "segment",
            "Operating segments",
            f"{surface} operates the {company.segment} segment. Segment management "
            "reviews revenue, gross margin, and operating investment together; the "
            "segment label is specific to this synthetic company and fiscal period.",
            "OPERATES_SEGMENT",
            segment_key,
        ),
        SectionSpec(
            "revenue",
            "Revenue",
            f"{surface} reported synthetic revenue of ${current.revenue_billions:.1f} "
            f"billion for fiscal year {year}, compared with ${prior.revenue_billions:.1f} "
            f"billion in fiscal year {year - 1}. These exact values, currency, units, "
            "and periods must remain attached to this source chunk.",
        ),
        SectionSpec(
            "margin",
            "Gross margin",
            f"{surface} reported a synthetic gross margin of "
            f"{current.gross_margin_percent:.1f}% for fiscal year {year}, versus "
            f"{prior.gross_margin_percent:.1f}% for fiscal year {year - 1}. Gross margin "
            "is a percentage and must not be rewritten as revenue or operating margin.",
        ),
        SectionSpec(
            "cash",
            "Liquidity",
            f"{surface} held synthetic cash and cash equivalents of "
            f"${current.cash_billions:.1f} billion at the end of fiscal year {year}. "
            "The amount excludes undrawn credit facilities, contracted backlog, and "
            "restricted balances described elsewhere in the synthetic report.",
        ),
        SectionSpec(
            "risk-market",
            "Market risk",
            f"{surface} identifies Macroeconomic volatility as a synthetic risk factor. "
            "Changes in demand, interest rates, inflation, and foreign-exchange conditions "
            "could affect reported results, but this paragraph makes no prediction about "
            "the probability or size of an effect.",
            "HAS_RISK",
            market_key,
        ),
        SectionSpec(
            "risk-supply",
            "Operational risk",
            f"{surface} identifies Supply-chain disruption as a synthetic risk factor. "
            "Supplier concentration, transport interruptions, and delayed components "
            "could constrain operations. The filing does not claim that a disruption "
            f"occurred during fiscal year {year}.",
            "HAS_RISK",
            supply_key,
        ),
        SectionSpec(
            "segment-detail",
            "Segment detail",
            f"{surface} assigns product delivery, customer support, and investment for "
            f"the {company.segment} segment to one accountable operating leader. This "
            "description supports navigation to the segment but does not independently "
            "state a financial total.",
            "OPERATES_SEGMENT",
            segment_key,
        ),
        SectionSpec(
            "capital",
            "Capital allocation",
            f"{surface} returned a synthetic ${current.capital_return_billions:.1f} "
            f"billion to stakeholders during fiscal year {year} through the combined "
            "illustrative mechanisms of distributions and repurchases. This value is not "
            "cash on hand and is not revenue.",
        ),
        SectionSpec(
            "temporal",
            "Temporal interpretation",
            f"{surface} states that every number in this synthetic filing is scoped to "
            f"fiscal year {year} unless the sentence names another period. A later-year "
            "filing may report a different value without making either source erroneous; "
            "retrieval must retain the period and source identity.",
        ),
    )


def _semantic_family(section_key: str) -> str:
    """Group multiple source chunks that substantively support one fact."""
    return {
        "business": "offering",
        "product": "offering",
        "segment": "segment",
        "segment-detail": "segment",
    }.get(section_key, section_key)


def _profile() -> tuple[str, tuple[str, ...]]:
    signatures = (
        "unicode-nfc-lf:v1",
        SPLITTER_SIGNATURE,
        EXTRACTOR_SIGNATURE,
        "checked-in-synthetic-annotations:v1",
        SCHEMA_SIGNATURE,
        f"dev-corpus-builder:{GENERATOR_VERSION}",
    )
    return pipeline_profile_id(*signatures), signatures


def _embedding_profile() -> dict[str, Any]:
    space_id = embedding_space_id(
        EMBEDDING_PROVIDER,
        EMBEDDING_MODEL,
        EMBEDDING_REVISION,
        EMBEDDING_DIMENSIONS,
        EMBEDDING_NORMALIZATION,
    )
    return {
        "dimensions": EMBEDDING_DIMENSIONS,
        "embedding_space_id": space_id,
        "fixture_method": "adjudicated_evidence_cluster_projection",
        "model": EMBEDDING_MODEL,
        "normalization": EMBEDDING_NORMALIZATION,
        "provider": EMBEDDING_PROVIDER,
        "quality_claim": "none",
        "revision": EMBEDDING_REVISION,
        "warning": (
            "Gold-derived fixture vectors validate retrieval orchestration only; "
            "they cannot demonstrate embedding-model or customer-corpus quality."
        ),
    }


def _question_records(
    chunks_by_key: dict[str, dict[str, Any]],
    companies_by_key: dict[str, CompanySpec],
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []

    def ck(company_key: str, year: int, section: str) -> str:
        company = companies_by_key[company_key]
        return f"{company.tenant_id}:{company.ticker.casefold()}:fy{year}:{section}"

    def fact(
        company_key: str,
        year: int,
        family: str,
    ) -> tuple[str, ...]:
        """Return every Chunk that substantively supports one fact in one year."""
        sections = {
            "offering": ("business", "product"),
            "segment": ("segment", "segment-detail"),
        }.get(family, (family,))
        return tuple(ck(company_key, year, section) for section in sections)

    def all_years(company_key: str, family: str) -> tuple[str, ...]:
        """Expand evidence across years only for questions without a named year."""
        return tuple(
            key
            for year in (2023, 2024)
            for key in fact(company_key, year, family)
        )

    def graded(keys: Iterable[str], grade: float = 3.0) -> tuple[tuple[str, float], ...]:
        return tuple((key, grade) for key in keys)

    def combine(*groups: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(key for group in groups for key in group))

    def add(
        question_class: str,
        case_type: str,
        number: int,
        query: str,
        *,
        tenant_id: str,
        groups: tuple[str, ...],
        relevance: tuple[tuple[str, float], ...] = (),
        forbidden: tuple[str, ...] = (),
    ) -> None:
        item_id = f"{question_class}-{case_type}-{number:02d}"
        relevance_by_key = dict(relevance)
        records.append(
            {
                "answerable": any(grade > 0 for grade in relevance_by_key.values()),
                "case_type": case_type,
                "forbidden_chunk_ids": [chunks_by_key[key]["chunk_id"] for key in forbidden],
                "forbidden_chunk_keys": list(forbidden),
                "id": item_id,
                "principal": {
                    "groups": list(groups),
                    "principal_id": f"fixture-{item_id}",
                    "tenant_id": tenant_id,
                },
                "query": query,
                "question_class": question_class,
                "relevance": {
                    chunks_by_key[key]["chunk_id"]: grade
                    for key, grade in relevance_by_key.items()
                },
                "relevance_chunk_keys": relevance_by_key,
                "vector_id": f"query:{item_id}",
            }
        )

    # Five success and two boundary cases per Stage 1 question class.
    add("single_chunk", "success", 1, "What was Northstar revenue in fiscal 2024?", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2024, "revenue"), 3),))
    add("single_chunk", "success", 2, "What gross margin did Meridian report for 2024?", tenant_id="tenant-beta", groups=("beta-public",), relevance=((ck("meridian-retail", 2024, "margin"), 3),))
    add("single_chunk", "success", 3, "How much cash did Harbor hold at the end of 2023?", tenant_id="tenant-beta", groups=("beta-board",), relevance=((ck("harbor-energy", 2023, "cash"), 3),))
    add("single_chunk", "success", 4, "What is Atlas Cloud's principal offering?", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=graded(all_years("atlas-cloud", "offering")))
    add("single_chunk", "success", 5, "Which segment does ticker ATL operate?", tenant_id="tenant-alpha", groups=("alpha-legal",), relevance=graded(all_years("atlas-logistics", "segment")))
    add("single_chunk", "boundary", 1, "NORTHSTAR revenue—FY2024?!", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2024, "revenue"), 3),))
    add("single_chunk", "boundary", 2, "For ATC, identify the reporting company called Atlas.", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=graded(all_years("atlas-cloud", "identity")))

    add("cross_chunk", "success", 1, "Compare Northstar's 2024 revenue and gross margin.", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2024, "revenue"), 3), (ck("northstar", 2024, "margin"), 3)))
    add("cross_chunk", "success", 2, "How did Northstar revenue change from 2023 to 2024?", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2023, "revenue"), 3), (ck("northstar", 2024, "revenue"), 3)))
    add("cross_chunk", "success", 3, "Give Atlas Cloud's offering and its market risk.", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=graded(combine(all_years("atlas-cloud", "offering"), all_years("atlas-cloud", "risk-market"))))
    add("cross_chunk", "success", 4, "Summarize Harbor's 2024 margin and cash.", tenant_id="tenant-beta", groups=("beta-board",), relevance=((ck("harbor-energy", 2024, "margin"), 3), (ck("harbor-energy", 2024, "cash"), 3)))
    add("cross_chunk", "success", 5, "Name the 2024 segments of both Atlas companies.", tenant_id="tenant-alpha", groups=("alpha-finance", "alpha-legal"), relevance=graded(combine(fact("atlas-cloud", 2024, "segment"), fact("atlas-logistics", 2024, "segment"))))
    add("cross_chunk", "boundary", 1, "Meridian: offering + year-end cash (FY24).", tenant_id="tenant-beta", groups=("beta-public",), relevance=graded(combine(fact("meridian-retail", 2024, "offering"), fact("meridian-retail", 2024, "cash"))))
    add("cross_chunk", "boundary", 2, "ATC versus ATL 2024 revenue; do not merge the names.", tenant_id="tenant-alpha", groups=("alpha-finance", "alpha-legal"), relevance=((ck("atlas-cloud", 2024, "revenue"), 3), (ck("atlas-logistics", 2024, "revenue"), 3)))

    add("graph_relationship", "success", 1, "What product does Northstar offer?", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=graded(all_years("northstar", "offering")))
    add("graph_relationship", "success", 2, "Which operational risk is linked to Atlas Cloud?", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=graded(all_years("atlas-cloud", "risk-supply")))
    add("graph_relationship", "success", 3, "Which segment is operated by Atlas Logistics?", tenant_id="tenant-alpha", groups=("alpha-legal",), relevance=graded(all_years("atlas-logistics", "segment")))
    add("graph_relationship", "success", 4, "Which market risk is shared by the two Atlas companies?", tenant_id="tenant-alpha", groups=("alpha-finance", "alpha-legal"), relevance=graded(combine(all_years("atlas-cloud", "risk-market"), all_years("atlas-logistics", "risk-market"))))
    add("graph_relationship", "success", 5, "Which supply-chain risk connects Meridian and Harbor?", tenant_id="tenant-beta", groups=("beta-public", "beta-board"), relevance=graded(combine(all_years("meridian-retail", "risk-supply"), all_years("harbor-energy", "risk-supply"))))
    add("graph_relationship", "boundary", 1, "northstar -> operates -> which segment?", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=graded(all_years("northstar", "segment")))
    add("graph_relationship", "boundary", 2, "Atlas offers what for ATC, and what for ATL?", tenant_id="tenant-alpha", groups=("alpha-finance", "alpha-legal"), relevance=graded(combine(all_years("atlas-cloud", "offering"), all_years("atlas-logistics", "offering"))))

    add("exact_value", "success", 1, "State Northstar's exact 2024 revenue with currency and units.", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2024, "revenue"), 3),))
    add("exact_value", "success", 2, "State Meridian's exact 2024 gross-margin percentage.", tenant_id="tenant-beta", groups=("beta-public",), relevance=((ck("meridian-retail", 2024, "margin"), 3),))
    add("exact_value", "success", 3, "State Harbor's exact 2023 cash balance with units.", tenant_id="tenant-beta", groups=("beta-board",), relevance=((ck("harbor-energy", 2023, "cash"), 3),))
    add("exact_value", "success", 4, "How much capital did Atlas Cloud return in 2024?", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=((ck("atlas-cloud", 2024, "capital"), 3),))
    add("exact_value", "success", 5, "What exact revenue did Atlas Logistics report for 2024?", tenant_id="tenant-alpha", groups=("alpha-legal",), relevance=((ck("atlas-logistics", 2024, "revenue"), 3),))
    add("exact_value", "boundary", 1, "Which ATC fact contains '$52.8 billion' in FY24?", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=((ck("atlas-cloud", 2024, "revenue"), 3),))
    add("exact_value", "boundary", 2, "Harbor FY2024: preserve 20.8% exactly.", tenant_id="tenant-beta", groups=("beta-board",), relevance=((ck("harbor-energy", 2024, "margin"), 3),))

    add("temporal_conflict", "success", 1, "Compare Northstar revenue in 2023 and 2024.", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2023, "revenue"), 3), (ck("northstar", 2024, "revenue"), 3)))
    add("temporal_conflict", "success", 2, "Compare Atlas Cloud margin in 2023 and 2024.", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=((ck("atlas-cloud", 2023, "margin"), 3), (ck("atlas-cloud", 2024, "margin"), 3)))
    add("temporal_conflict", "success", 3, "How did Atlas Logistics cash change between 2023 and 2024?", tenant_id="tenant-alpha", groups=("alpha-legal",), relevance=((ck("atlas-logistics", 2023, "cash"), 3), (ck("atlas-logistics", 2024, "cash"), 3)))
    add("temporal_conflict", "success", 4, "Give Meridian revenue for both available fiscal years.", tenant_id="tenant-beta", groups=("beta-public",), relevance=((ck("meridian-retail", 2023, "revenue"), 3), (ck("meridian-retail", 2024, "revenue"), 3)))
    add("temporal_conflict", "success", 5, "Did Harbor revenue rise or fall from 2023 to 2024?", tenant_id="tenant-beta", groups=("beta-board",), relevance=((ck("harbor-energy", 2023, "revenue"), 3), (ck("harbor-energy", 2024, "revenue"), 3)))
    add("temporal_conflict", "boundary", 1, "Northstar current FY2024 revenue only, not FY2023.", tenant_id="tenant-alpha", groups=("alpha-public",), relevance=((ck("northstar", 2024, "revenue"), 3), (ck("northstar", 2023, "revenue"), 0)))
    add("temporal_conflict", "boundary", 2, "Atlas Cloud FY2023 margin, excluding later FY2024.", tenant_id="tenant-alpha", groups=("alpha-finance",), relevance=((ck("atlas-cloud", 2023, "margin"), 3), (ck("atlas-cloud", 2024, "margin"), 0)))

    unanswerable = (
        "What were Northstar's audited carbon emissions?",
        "How many employees did Atlas Cloud have?",
        "Which cities host Atlas Logistics warehouses?",
        "What audit fee did Meridian pay?",
        "Which exchange rate assumption did Harbor use?",
        "What was ticker XYZ's revenue?",
        "On which exact day was a dividend paid?",
    )
    for index, query in enumerate(unanswerable, start=1):
        add("unanswerable", "success" if index <= 5 else "boundary", index if index <= 5 else index - 5, query, tenant_id="tenant-alpha", groups=("alpha-public",))

    add("unauthorized", "success", 1, "What was Atlas Cloud revenue in 2024?", tenant_id="tenant-alpha", groups=("alpha-public",), forbidden=(ck("atlas-cloud", 2024, "revenue"),))
    add("unauthorized", "success", 2, "What does Atlas Logistics offer?", tenant_id="tenant-alpha", groups=("alpha-finance",), forbidden=all_years("atlas-logistics", "offering"))
    add("unauthorized", "success", 3, "How much cash did Harbor hold in 2024?", tenant_id="tenant-beta", groups=("beta-public",), forbidden=(ck("harbor-energy", 2024, "cash"),))
    add("unauthorized", "success", 4, "What gross margin did Meridian report?", tenant_id="tenant-beta", groups=("beta-board",), forbidden=all_years("meridian-retail", "margin"))
    add("unauthorized", "success", 5, "Compare the two protected Atlas companies.", tenant_id="tenant-alpha", groups=("alpha-public",), forbidden=combine(all_years("atlas-cloud", "identity"), all_years("atlas-logistics", "identity")))
    add("unauthorized", "boundary", 1, "Does tenant alpha Northstar exist?", tenant_id="tenant-beta", groups=("beta-public",), forbidden=all_years("northstar", "identity"))
    add("unauthorized", "boundary", 2, "Return the protected exact value $52.8 billion.", tenant_id="tenant-alpha", groups=("alpha-legal",), forbidden=(ck("atlas-cloud", 2024, "revenue"),))

    return tuple(records)


def build_dataset() -> DatasetBuild:
    profile_id, signatures = _profile()
    embedding_profile = _embedding_profile()
    companies_by_key = {company.key: company for company in COMPANIES}

    entity_map: dict[str, dict[str, Any]] = {}
    for company in COMPANIES:
        for record in (
            _company_entity(company),
            _product_entity(company),
            _segment_entity(company),
        ):
            entity_map.setdefault(record["entity_key"], record)
    for tenant_id in sorted({company.tenant_id for company in COMPANIES}):
        for record in (
            _risk_entity(tenant_id, "macroeconomic-volatility", "Macroeconomic volatility"),
            _risk_entity(tenant_id, "supply-chain-disruption", "Supply-chain disruption"),
        ):
            entity_map.setdefault(record["entity_key"], record)

    source_files: dict[str, bytes] = {}
    documents: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    chunks_by_key: dict[str, dict[str, Any]] = {}

    for company in COMPANIES:
        company_record = _company_entity(company)
        for year in (2023, 2024):
            sections = _section_specs(company, year)
            identity_anchor = (
                f"{company.canonical_name} ({company.ticker}), fiscal year {year} — "
            )
            section_texts = tuple(
                (
                    section.text
                    if section.key == "identity"
                    else f"{identity_anchor}{section.text}"
                )
                + ("\n" if index == len(sections) - 1 else "\n\n")
                for index, section in enumerate(sections)
            )
            normalized_text = "".join(section_texts)
            source_bytes = normalized_text.encode("utf-8")
            source_path = f"sources/{company.tenant_id}/{company.ticker.casefold()}-fy{year}.txt"
            source_files[source_path] = source_bytes
            canonical_uri = canonicalize_uri(
                f"urn:sample-graphrag:synthetic:{DATASET_ID}:"
                f"{company.tenant_id}:{company.ticker.casefold()}:fy{year}"
            )
            document_identifier = document_id(company.tenant_id, canonical_uri)
            checksum = content_checksum(source_bytes)
            version_identifier = version_id(document_identifier, checksum, checksum)
            document_key = f"{company.tenant_id}:{company.ticker.casefold()}:fy{year}"
            published_at = f"{year}-11-15T00:00:00+00:00"
            documents.append(
                {
                    "access_groups": [company.access_group],
                    "access_policy_id": f"{company.tenant_id}:{company.access_group}",
                    "access_policy_version": 1,
                    "canonical_uri": canonical_uri,
                    "checksum": checksum,
                    "chunk_count": len(sections),
                    "company_canonical_name": company.canonical_name,
                    "company_entity_id": company_record["entity_id"],
                    "company_key": company.key,
                    "created_at": FIXED_INGESTED_AT,
                    "document_id": document_identifier,
                    "document_key": document_key,
                    "fiscal_year": year,
                    "ingested_at": FIXED_INGESTED_AT,
                    "identity_anchor": identity_anchor,
                    "language": "en",
                    "mime_type": "text/plain",
                    "original_checksum": checksum,
                    "published_at": published_at,
                    "source_name": "sample-graphrag deterministic synthetic filing",
                    "source_path": source_path,
                    "tenant_id": company.tenant_id,
                    "ticker": company.ticker,
                    "title": f"{company.canonical_name} synthetic FY{year} filing",
                    "version_id": version_identifier,
                    "version_number": 1,
                }
            )

            char_start = 0
            for ordinal, (section, text) in enumerate(zip(sections, section_texts)):
                char_end = char_start + len(text)
                chunk_checksum = content_checksum(text)
                chunk_identifier = chunk_id(
                    version_identifier,
                    SPLITTER_SIGNATURE,
                    ordinal,
                    char_start,
                    char_end,
                    chunk_checksum,
                )
                chunk_key = f"{document_key}:{section.key}"
                company_relative_start = text.index(company.canonical_name)
                company_start = char_start + company_relative_start
                company_end = company_start + len(company.canonical_name)
                mentions = [
                    {
                        "confidence": 1.0,
                        "entity_id": company_record["entity_id"],
                        "entity_key": company_record["entity_key"],
                        "entity_type": "Company",
                        "extractor_version": EXTRACTOR_SIGNATURE,
                        "char_end": company_end,
                        "char_start": company_start,
                        "mention_id": mention_id(
                            chunk_identifier,
                            "Company",
                            company_start,
                            company_end,
                            company.canonical_name,
                            EXTRACTOR_SIGNATURE,
                        ),
                        "surface": company.canonical_name,
                    }
                ]
                assertions: list[dict[str, Any]] = []
                if section.object_entity_key is not None:
                    object_record = entity_map[section.object_entity_key]
                    object_surface = object_record["canonical_name"]
                    object_relative_start = text.index(object_surface)
                    object_start = char_start + object_relative_start
                    object_end = object_start + len(object_surface)
                    mentions.append(
                        {
                            "confidence": 1.0,
                            "entity_id": object_record["entity_id"],
                            "entity_key": object_record["entity_key"],
                            "entity_type": object_record["entity_type"],
                            "extractor_version": EXTRACTOR_SIGNATURE,
                            "char_end": object_end,
                            "char_start": object_start,
                            "mention_id": mention_id(
                                chunk_identifier,
                                object_record["entity_type"],
                                object_start,
                                object_end,
                                object_surface,
                                EXTRACTOR_SIGNATURE,
                            ),
                            "surface": object_surface,
                        }
                    )
                    if section.predicate is None:
                        raise AssertionError("relationship object requires a predicate")
                    assertions.append(
                        {
                            "accepted": True,
                            "assertion_id": assertion_id(
                                company.tenant_id,
                                company_record["entity_id"],
                                section.predicate,
                                "entity",
                                object_record["entity_id"],
                                chunk_identifier,
                                char_start,
                                char_end,
                                EXTRACTOR_SIGNATURE,
                                SCHEMA_SIGNATURE,
                            ),
                            "confidence": 1.0,
                            "evidence_char_end": char_end,
                            "evidence_char_start": char_start,
                            "evidence_chunk_id": chunk_identifier,
                            "extractor_version": EXTRACTOR_SIGNATURE,
                            "literal_value": None,
                            "object_entity_id": object_record["entity_id"],
                            "object_entity_key": object_record["entity_key"],
                            "predicate": section.predicate,
                            "schema_version": SCHEMA_SIGNATURE,
                            "subject_entity_id": company_record["entity_id"],
                            "subject_entity_key": company_record["entity_key"],
                            "tenant_id": company.tenant_id,
                        }
                    )
                record = {
                    "access_groups": [company.access_group],
                    "access_policy_id": f"{company.tenant_id}:{company.access_group}",
                    "access_policy_version": 1,
                    "assertions": assertions,
                    "char_end": char_end,
                    "char_start": char_start,
                    "checksum": chunk_checksum,
                    "chunk_id": chunk_identifier,
                    "chunk_key": chunk_key,
                    "document_id": document_identifier,
                    "document_key": document_key,
                    "mentions": sorted(mentions, key=lambda item: (item["char_start"], item["entity_id"])),
                    "ordinal": ordinal,
                    "page_number": ordinal // 3 + 1,
                    "section": section.title,
                    "semantic_cluster": (
                        f"{company.tenant_id}:{company.key}:fy{year}:"
                        f"{_semantic_family(section.key)}"
                    ),
                    "source_path": source_path,
                    "splitter_version": SPLITTER_SIGNATURE,
                    "tenant_id": company.tenant_id,
                    "text_length": len(text),
                    "version_id": version_identifier,
                }
                chunks.append(record)
                chunks_by_key[chunk_key] = record
                char_start = char_end

    questions = list(_question_records(chunks_by_key, companies_by_key))
    chunks_by_id = {item["chunk_id"]: item for item in chunks}
    unanswerable_features = {
        item["id"]: f"unanswerable:{item['id']}"
        for item in questions
        if item["question_class"] == "unanswerable"
    }
    feature_names = sorted(
        {
            item["semantic_cluster"]
            for item in chunks
        }
        | set(unanswerable_features.values())
    )
    if len(feature_names) > EMBEDDING_DIMENSIONS:
        raise ValueError("fixture semantic features exceed embedding dimensions")
    feature_indices = {
        feature: index for index, feature in enumerate(feature_names)
    }
    vectors: list[dict[str, Any]] = []
    for chunk in chunks:
        feature = chunk["semantic_cluster"]
        vectors.append(
            {
                "id": chunk["chunk_id"],
                "kind": "chunk",
                "logical_key": chunk["chunk_key"],
                "semantic_features": [feature],
                "vector": list(_feature_vector({feature: 1.0}, feature_indices)),
            }
        )
    for item in questions:
        feature_weights: dict[str, float] = {}
        if item["answerable"]:
            for chunk_id_value, grade in item["relevance"].items():
                if grade <= 0:
                    continue
                feature = chunks_by_id[chunk_id_value]["semantic_cluster"]
                feature_weights[feature] = max(feature_weights.get(feature, 0.0), grade)
        elif item["question_class"] == "unauthorized":
            for chunk_id_value in item["forbidden_chunk_ids"]:
                feature = chunks_by_id[chunk_id_value]["semantic_cluster"]
                feature_weights[feature] = 1.0
        else:
            feature_weights[unanswerable_features[item["id"]]] = 1.0
        vectors.append(
            {
                "id": item["vector_id"],
                "kind": "query",
                "logical_key": item["id"],
                "semantic_features": sorted(feature_weights),
                "vector": list(_feature_vector(feature_weights, feature_indices)),
            }
        )
    embedding_profile["feature_count"] = len(feature_names)
    embedding_profile["feature_index_checksum"] = content_checksum(
        json.dumps(feature_names, separators=(",", ":"), ensure_ascii=False)
    )

    entities = sorted(entity_map.values(), key=lambda item: item["entity_key"])
    documents.sort(key=lambda item: item["document_key"])
    chunks.sort(key=lambda item: item["chunk_key"])
    questions.sort(key=lambda item: item["id"])
    vectors.sort(key=lambda item: (item["kind"], item["logical_key"]))

    notice = (
        "SYNTHETIC DEVELOPMENT DATA ONLY\n"
        "\n"
        "Every company, filing, value, relationship, and question in this directory is\n"
        "fabricated for deterministic GraphRAG development. Nothing here is a real SEC\n"
        "filing, investment fact, customer record, or production validation result.\n"
    ).encode("utf-8")
    generated: dict[str, bytes] = {
        "NOTICE.txt": notice,
        "chunks.jsonl": _jsonl_bytes(chunks),
        "entities.jsonl": _jsonl_bytes(entities),
        "questions.jsonl": _jsonl_bytes(questions),
        "vectors.jsonl": _jsonl_bytes(vectors),
        **source_files,
    }
    homonym_pair = (
        _company_entity(companies_by_key["atlas-cloud"]),
        _company_entity(companies_by_key["atlas-logistics"]),
    )
    profile_fields = (
        "normalizer_signature",
        "splitter_signature",
        "extractor_signature",
        "prompt_signature",
        "schema_signature",
        "code_signature",
    )
    manifest = {
        "artifacts": [
            {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
            for path, payload in sorted(generated.items())
        ],
        "counts": {
            "active_chunks": len(chunks),
            "companies": len(COMPANIES),
            "documents": len(documents),
            "entities": len(entities),
            "questions": len(questions),
            "tenants": len({company.tenant_id for company in COMPANIES}),
            "vectors": len(vectors),
        },
        "coverage": {
            "access_groups": sorted({company.access_group for company in COMPANIES}),
            "fiscal_years": [2023, 2024],
            "homonym_negative_pairs": [
                {
                    "entity_ids": [homonym_pair[0]["entity_id"], homonym_pair[1]["entity_id"]],
                    "entity_keys": [homonym_pair[0]["entity_key"], homonym_pair[1]["entity_key"]],
                    "expected_resolution": "KEEP_SEPARATE",
                    "shared_surface": "Atlas",
                    "tenant_id": "tenant-alpha",
                }
            ],
            "question_classes": sorted({item["question_class"] for item in questions}),
            "tenants": sorted({company.tenant_id for company in COMPANIES}),
        },
        "dataset_id": DATASET_ID,
        "description": "Deterministic representative synthetic company-filing development corpus.",
        "documents": documents,
        "embedding_profile": embedding_profile,
        "fixture_vector_scope": {
            "can_validate": [
                "retrieval orchestration",
                "ranking fusion over controlled semantic candidates",
                "authorization filtering",
                "citation and trace stability",
            ],
            "cannot_validate": [
                "embedding model quality",
                "natural-language generalization",
                "customer-corpus retrieval quality",
                "production-candidate quality",
            ],
            "query_mapping": (
                "Answerable queries project adjudicated positive evidence clusters; "
                "unauthorized queries project forbidden evidence clusters; "
                "unanswerable queries use an unanchored reserved feature."
            ),
        },
        "generated_by": {
            "path": "scripts/build_dev_corpus.py",
            "version": GENERATOR_VERSION,
        },
        "owner": "repository-maintainers",
        "pipeline_profile": {
            "profile_id": profile_id,
            **dict(zip(profile_fields, signatures)),
        },
        "synthetic": True,
        "version": DATASET_VERSION,
        "warning": "Not real filings and not production-candidate validation evidence.",
    }
    generated["manifest.json"] = _json_bytes(manifest)
    build = DatasetBuild(
        files=dict(sorted(generated.items())),
        manifest=manifest,
        documents=tuple(documents),
        entities=tuple(entities),
        chunks=tuple(chunks),
        questions=tuple(questions),
        vectors=tuple(vectors),
    )
    errors = validate_build(build)
    if errors:
        raise ValueError("invalid generated development corpus: " + "; ".join(errors))
    return build


def validate_build(build: DatasetBuild) -> tuple[str, ...]:
    errors: list[str] = []
    if build.manifest.get("synthetic") is not True:
        errors.append("manifest must explicitly mark the corpus synthetic")
    counts = build.manifest.get("counts", {})
    if not 100 <= counts.get("active_chunks", 0) <= 200:
        errors.append("active chunk count must be between 100 and 200")
    if counts.get("active_chunks") != len(build.chunks):
        errors.append("manifest active chunk count does not match chunks")
    if counts.get("documents") != len(build.documents):
        errors.append("manifest document count does not match documents")
    if counts.get("companies") != 5:
        errors.append("corpus must contain exactly five synthetic companies")
    if counts.get("tenants") != 2:
        errors.append("corpus must contain exactly two tenants")
    if len(build.questions) != 49:
        errors.append("corpus must contain 49 representative questions")

    expected_classes = {
        "single_chunk",
        "cross_chunk",
        "graph_relationship",
        "exact_value",
        "temporal_conflict",
        "unanswerable",
        "unauthorized",
    }
    quotas = Counter((item["question_class"], item["case_type"]) for item in build.questions)
    for question_class in expected_classes:
        if quotas[(question_class, "success")] != 5:
            errors.append(f"{question_class} must contain five success questions")
        if quotas[(question_class, "boundary")] != 2:
            errors.append(f"{question_class} must contain two boundary questions")

    source_texts = {
        path: payload.decode("utf-8")
        for path, payload in build.files.items()
        if path.startswith("sources/")
    }
    documents_by_key = {item["document_key"]: item for item in build.documents}
    chunks_by_id = {item["chunk_id"]: item for item in build.chunks}
    if len(documents_by_key) != len(build.documents):
        errors.append("document keys must be unique")
    if len(chunks_by_id) != len(build.chunks):
        errors.append("chunk IDs must be unique")

    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in build.chunks:
        chunks_by_document.setdefault(chunk["document_key"], []).append(chunk)
    for document_key, document in documents_by_key.items():
        source = source_texts.get(document["source_path"])
        if source is None:
            errors.append(f"missing source file for {document_key}")
            continue
        if content_checksum(source) != document["checksum"]:
            errors.append(f"source checksum mismatch for {document_key}")
        document_chunks = sorted(chunks_by_document.get(document_key, ()), key=lambda item: item["ordinal"])
        if len(document_chunks) != document["chunk_count"]:
            errors.append(f"chunk count mismatch for {document_key}")
            continue
        cursor = 0
        for ordinal, chunk in enumerate(document_chunks):
            if chunk["ordinal"] != ordinal or chunk["char_start"] != cursor:
                errors.append(f"non-contiguous chunks for {document_key}")
                break
            text = source[chunk["char_start"] : chunk["char_end"]]
            if len(text) != chunk["text_length"] or content_checksum(text) != chunk["checksum"]:
                errors.append(f"chunk source round trip failed for {chunk['chunk_key']}")
            identity_values = (
                document["company_canonical_name"],
                f"({document['ticker']})",
                f"fiscal year {document['fiscal_year']}",
            )
            if any(value not in text for value in identity_values):
                errors.append(f"chunk lacks a self-contained identity anchor: {chunk['chunk_key']}")
            if ordinal > 0 and not text.startswith(document["identity_anchor"]):
                errors.append(f"chunk has a malformed identity anchor: {chunk['chunk_key']}")
            company_mentions = [
                mention
                for mention in chunk["mentions"]
                if mention["entity_id"] == document["company_entity_id"]
            ]
            if len(company_mentions) != 1 or company_mentions[0]["surface"] != document["company_canonical_name"]:
                errors.append(f"chunk lacks one canonical company mention: {chunk['chunk_key']}")
            for mention in chunk["mentions"]:
                if source[mention["char_start"] : mention["char_end"]] != mention["surface"]:
                    errors.append(f"mention source round trip failed for {mention['mention_id']}")
            cursor = chunk["char_end"]
        if cursor != len(source):
            errors.append(f"chunks do not cover the complete source for {document_key}")

    vectors_by_id = {item["id"]: item for item in build.vectors}
    vector_ids = set(vectors_by_id)
    chunk_semantic_features = {
        chunk["semantic_cluster"] for chunk in build.chunks
    }
    for vector in build.vectors:
        values = vector["vector"]
        if len(values) != EMBEDDING_DIMENSIONS:
            errors.append(f"vector {vector['id']} has the wrong dimensions")
        elif not all(math.isfinite(value) for value in values):
            errors.append(f"vector {vector['id']} contains a non-finite value")
        elif math.isclose(math.sqrt(math.fsum(value * value for value in values)), 0.0):
            errors.append(f"vector {vector['id']} has zero norm")
        if not vector.get("semantic_features"):
            errors.append(f"vector {vector['id']} has no auditable semantic features")
    chunk_ids = set(chunks_by_id)
    chunks_by_key = {item["chunk_key"]: item for item in build.chunks}
    for item in build.questions:
        if item["vector_id"] not in vector_ids:
            errors.append(f"question {item['id']} has no query vector")
            query_features: set[str] = set()
        else:
            query_features = set(vectors_by_id[item["vector_id"]]["semantic_features"])
        if not set(item["relevance"]) <= chunk_ids:
            errors.append(f"question {item['id']} references an unknown relevant chunk")
        if not set(item["forbidden_chunk_ids"]) <= chunk_ids:
            errors.append(f"question {item['id']} references an unknown forbidden chunk")
        positive = [chunk_id_value for chunk_id_value, grade in item["relevance"].items() if grade > 0]
        if item["answerable"] != bool(positive):
            errors.append(f"question {item['id']} has inconsistent answerability")
        expected_query_features = {
            chunks_by_id[chunk_id_value]["semantic_cluster"]
            for chunk_id_value in positive
        }
        if item["question_class"] == "unauthorized":
            expected_query_features = {
                chunks_by_id[chunk_id_value]["semantic_cluster"]
                for chunk_id_value in item["forbidden_chunk_ids"]
            }
        elif item["question_class"] == "unanswerable":
            expected_query_features = {f"unanswerable:{item['id']}"}
            if query_features & chunk_semantic_features:
                errors.append(f"unanswerable question {item['id']} is anchored to corpus evidence")
        if query_features != expected_query_features:
            errors.append(f"question {item['id']} fixture vector does not match its adjudication")
        principal = item["principal"]
        for chunk_id_value in positive:
            chunk = chunks_by_id[chunk_id_value]
            if chunk["tenant_id"] != principal["tenant_id"] or not set(chunk["access_groups"]) & set(principal["groups"]):
                errors.append(f"answerable question {item['id']} lacks source authorization")
        if item["question_class"] == "unauthorized":
            if not item["forbidden_chunk_keys"]:
                errors.append(f"unauthorized question {item['id']} requires forbidden evidence")
            for key in item["forbidden_chunk_keys"]:
                chunk = chunks_by_key[key]
                if chunk["tenant_id"] == principal["tenant_id"] and set(chunk["access_groups"]) & set(principal["groups"]):
                    errors.append(f"unauthorized question {item['id']} can access forbidden evidence")

    pairs = build.manifest.get("coverage", {}).get("homonym_negative_pairs", [])
    if len(pairs) != 1:
        errors.append("corpus must define one homonym negative pair")
    elif len(set(pairs[0].get("entity_ids", ()))) != 2:
        errors.append("homonym pair must resolve to two distinct stable entity IDs")
    return tuple(errors)


def check_dataset(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    build: DatasetBuild | None = None,
) -> tuple[str, ...]:
    expected = (build or build_dataset()).files
    if not dataset_dir.is_dir():
        return (f"dataset directory is missing: {dataset_dir}",)
    actual_paths = {
        path.relative_to(dataset_dir).as_posix()
        for path in dataset_dir.rglob("*")
        if path.is_file()
    }
    expected_paths = set(expected)
    errors: list[str] = []
    for path in sorted(expected_paths - actual_paths):
        errors.append(f"missing generated file: {path}")
    for path in sorted(actual_paths - expected_paths):
        errors.append(f"unexpected generated file: {path}")
    for relative_path in sorted(expected_paths & actual_paths):
        actual = (dataset_dir / relative_path).read_bytes()
        if actual != expected[relative_path]:
            errors.append(f"generated file drifted: {relative_path}")
    return tuple(errors)


def write_dataset(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    build: DatasetBuild | None = None,
) -> None:
    generated = build or build_dataset()
    existing_paths: set[str] = set()
    if dataset_dir.exists():
        existing_paths = {
            path.relative_to(dataset_dir).as_posix()
            for path in dataset_dir.rglob("*")
            if path.is_file()
        }
    unexpected = existing_paths - set(generated.files)
    if unexpected:
        raise ValueError(
            "refusing to remove unexpected dataset files: " + ", ".join(sorted(unexpected))
        )
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, payload in generated.files.items():
        destination = dataset_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_name = handle.name
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
    errors = check_dataset(dataset_dir, generated)
    if errors:
        raise RuntimeError("written dataset failed verification: " + "; ".join(errors))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="verify checked-in bytes")
    action.add_argument("--write", action="store_true", help="write deterministic bytes")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=DEFAULT_DATASET_DIR,
        help="override the generated dataset directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    build = build_dataset()
    if args.write:
        write_dataset(args.dataset_dir, build)
        print(
            f"wrote {build.manifest['counts']['active_chunks']} chunks to "
            f"{args.dataset_dir}"
        )
        return 0
    errors = check_dataset(args.dataset_dir, build)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(
        f"verified {build.manifest['counts']['active_chunks']} chunks in "
        f"{args.dataset_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
