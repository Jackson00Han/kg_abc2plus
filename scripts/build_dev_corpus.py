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
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
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
ANSWER_GOLD_VERSION = "1.1.0"
GENERATOR_VERSION = "1.3.0"
FIXED_INGESTED_AT = "2026-09-01T00:00:00+00:00"
SPLITTER_SIGNATURE = "synthetic-section-splitter:v1"
EXTRACTOR_SIGNATURE = "synthetic-adjudicated-extractor:v1"
SCHEMA_SIGNATURE = "company-filings:v1"
EMBEDDING_DIMENSIONS = 128
EMBEDDING_PROVIDER = "fixture"
EMBEDDING_MODEL = "adjudicated-evidence-clusters"
EMBEDDING_REVISION = "dev-corpus-v1.1"
EMBEDDING_NORMALIZATION = "l2"
_ANSWER_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_ANSWER_QUANTITY = re.compile(
    r"^(?P<currency>\$)?(?P<number>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<scale>billion))?(?P<percent>%)?$"
)
_ANSWER_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "as",
        "at",
        "be",
        "for",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "that",
        "the",
        "this",
        "was",
        "were",
        "with",
    }
)
_ANSWER_INFERENCE_TERMS = frozenset(
    {
        "compared",
        "comparison",
        "decrease",
        "decreased",
        "decreases",
        "difference",
        "equal",
        "from",
        "higher",
        "increase",
        "increased",
        "increases",
        "lower",
        "same",
        "to",
        "unchanged",
    }
)


def _answer_content_sequence(text: str) -> tuple[str, ...]:
    return tuple(
        token.casefold()
        for token in _ANSWER_TOKEN.findall(text)
        if token.casefold() not in _ANSWER_STOPWORDS
    )


def _answer_content_tokens(text: str) -> set[str]:
    """Mirror the Stage 6 sourced-wording gate for offline gold validation."""
    return set(_answer_content_sequence(text))


def _is_ordered_subsequence(
    needle: tuple[str, ...],
    haystack: tuple[str, ...],
) -> bool:
    if not needle:
        return False
    position = 0
    for token in haystack:
        if token == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def _answer_quantity_parts(value: str) -> tuple[Decimal, str] | None:
    match = _ANSWER_QUANTITY.fullmatch(value)
    if match is None:
        return None
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:
        return None
    qualifier = "".join(
        (
            match.group("currency") or "",
            f" {match.group('scale')}" if match.group("scale") else "",
            match.group("percent") or "",
        )
    )
    return number, qualifier


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
    answers: tuple[dict[str, Any], ...]
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


def _answer_gold_records(
    questions: Iterable[dict[str, Any]],
    chunks_by_key: dict[str, dict[str, Any]],
    documents: Iterable[dict[str, Any]],
    companies_by_key: dict[str, CompanySpec],
) -> tuple[dict[str, Any], ...]:
    """Create versioned grounded-answer annotations, never model predictions."""
    questions_by_id = {item["id"]: item for item in questions}
    documents_by_id = {item["document_id"]: item for item in documents}
    specs: dict[str, dict[str, Any]] = {}

    def ck(company_key: str, year: int, section: str) -> str:
        company = companies_by_key[company_key]
        return f"{company.tenant_id}:{company.ticker.casefold()}:fy{year}:{section}"

    def fact(
        company_key: str,
        years: Iterable[int],
        family: str,
    ) -> tuple[str, ...]:
        sections = {
            # Answer evidence is narrower than retrieval relevance: related
            # Chunks remain recall candidates, but only direct assertions may
            # be cited as support for the generated claim.
            "offering": ("business",),
            "segment": ("segment",),
        }.get(family, (family,))
        return tuple(
            ck(company_key, year, section)
            for year in years
            for section in sections
        )

    def evidence(keys: Iterable[str]) -> tuple[dict[str, Any], ...]:
        result: list[dict[str, Any]] = []
        for key in dict.fromkeys(keys):
            chunk = chunks_by_key[key]
            document = documents_by_id[chunk["document_id"]]
            result.append(
                {
                    "canonical_uri": document["canonical_uri"],
                    "char_end": chunk["char_end"],
                    "char_start": chunk["char_start"],
                    "chunk_checksum": chunk["checksum"],
                    "chunk_id": chunk["chunk_id"],
                    "chunk_key": key,
                    "document_id": chunk["document_id"],
                    "document_title": document["title"],
                    "ordinal": chunk["ordinal"],
                    "page_number": chunk["page_number"],
                    "published_at": document["published_at"],
                    "section": chunk["section"],
                    "source_name": document["source_name"],
                    "version_checksum": document["checksum"],
                    "version_id": chunk["version_id"],
                    "version_number": document["version_number"],
                }
            )
        return tuple(result)

    def claim(
        text: str,
        required_terms: Iterable[str],
        evidence_keys: Iterable[str],
        *,
        exact_tokens: Iterable[str] = (),
        inference: bool = False,
    ) -> dict[str, Any]:
        keys = tuple(dict.fromkeys(evidence_keys))
        return {
            "evidence_chunk_ids": [chunks_by_key[key]["chunk_id"] for key in keys],
            "evidence_chunk_keys": list(keys),
            "exact_tokens": list(dict.fromkeys(exact_tokens)),
            "inference": inference,
            "material": True,
            "reference_text": text,
            "required_terms": list(dict.fromkeys(required_terms)),
        }

    def metric_value(company_key: str, year: int, metric: str) -> str:
        company = companies_by_key[company_key]
        values = company.metrics_for(year)
        if metric == "revenue":
            return f"${values.revenue_billions:.1f} billion"
        if metric == "margin":
            return f"{values.gross_margin_percent:.1f}%"
        if metric == "cash":
            return f"${values.cash_billions:.1f} billion"
        if metric == "capital":
            return f"${values.capital_return_billions:.1f} billion"
        raise ValueError(f"unknown answer metric: {metric}")

    def metric_claim(company_key: str, year: int, metric: str) -> dict[str, Any]:
        company = companies_by_key[company_key]
        value = metric_value(company_key, year, metric)
        if metric == "revenue":
            label = "synthetic revenue"
            text = (
                f"{company.canonical_name} reported {label} of {value} "
                f"for fiscal year {year}."
            )
        elif metric == "margin":
            label = "synthetic gross margin"
            text = (
                f"{company.canonical_name} reported a {label} of {value} "
                f"for fiscal year {year}."
            )
        elif metric == "cash":
            label = "synthetic cash and cash equivalents"
            text = (
                f"{company.canonical_name} held {label} of {value} at the end "
                f"of fiscal year {year}."
            )
        elif metric == "capital":
            label = "returned"
            text = (
                f"{company.canonical_name} returned a synthetic {value} to stakeholders "
                f"during fiscal year {year}."
            )
        else:
            raise ValueError(f"unknown answer metric: {metric}")
        period = f"fiscal year {year}"
        return claim(
            text,
            (company.canonical_name, label, value, period),
            fact(company_key, (year,), metric),
            exact_tokens=(value, period),
        )

    def offering_claim(company_key: str, years: Iterable[int]) -> dict[str, Any]:
        company = companies_by_key[company_key]
        return claim(
            f"{company.canonical_name} describes a synthetic business centered on its {company.product}.",
            (
                company.canonical_name,
                "describes",
                "synthetic business",
                "centered",
                company.product,
            ),
            fact(company_key, years, "offering"),
        )

    def segment_claim(company_key: str, years: Iterable[int]) -> dict[str, Any]:
        company = companies_by_key[company_key]
        return claim(
            f"{company.canonical_name} operates the {company.segment} segment.",
            (company.canonical_name, "operates", company.segment),
            fact(company_key, years, "segment"),
        )

    def risk_claim(
        company_key: str,
        years: Iterable[int],
        risk: str,
    ) -> dict[str, Any]:
        company = companies_by_key[company_key]
        risk_name = {
            "risk-market": "Macroeconomic volatility",
            "risk-supply": "Supply-chain disruption",
        }[risk]
        return claim(
            f"{company.canonical_name} identifies {risk_name} as a synthetic risk factor.",
            (company.canonical_name, risk_name, "risk factor"),
            fact(company_key, years, risk),
        )

    def identity_claim(company_key: str, years: Iterable[int]) -> dict[str, Any]:
        company = companies_by_key[company_key]
        return claim(
            f"{company.surface} ({company.ticker}) identifies {company.canonical_name} as the reporting company.",
            (
                company.surface,
                company.ticker,
                "identifies",
                company.canonical_name,
                "reporting company",
            ),
            fact(company_key, years, "identity"),
        )

    def inference_claim(
        company_key: str,
        metric: str,
        direction: str,
    ) -> dict[str, Any]:
        company = companies_by_key[company_key]
        result = claim(
            f"{metric.capitalize()} for {company.canonical_name} {direction} from fiscal year 2023 to fiscal year 2024.",
            (company.canonical_name, metric, direction),
            (
                *fact(company_key, (2023,), metric),
                *fact(company_key, (2024,), metric),
            ),
            inference=True,
        )
        result["comparison"] = {
            "direction": direction,
            "from_period": "fiscal year 2023",
            "from_value": metric_value(company_key, 2023, metric),
            "to_period": "fiscal year 2024",
            "to_value": metric_value(company_key, 2024, metric),
        }
        return result

    def shared_risk_claims(
        first_key: str,
        second_key: str,
        risk: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            risk_claim(first_key, (2023, 2024), risk),
            risk_claim(second_key, (2023, 2024), risk),
        )

    def answer(
        item_id: str,
        claims: Iterable[dict[str, Any]],
        *,
        expected_status: str = "answered",
        forbidden_answer_terms: Iterable[str] = (),
        conflict: dict[str, Any] | None = None,
        temporal_comparison: dict[str, Any] | None = None,
    ) -> None:
        material_claims = tuple(claims)
        specs[item_id] = {
            "claims": material_claims,
            "conflict": conflict,
            "expected_status": expected_status,
            "forbidden_answer_terms": list(dict.fromkeys(forbidden_answer_terms)),
            "reference_answer": " ".join(
                ("Inference: " if item["inference"] else "")
                + item["reference_text"]
                for item in material_claims
            ),
            "refusal_reason": None,
            "temporal_comparison": temporal_comparison,
        }

    def refuse(
        item_id: str,
        reason: str,
        *,
        protected_terms: Iterable[str] = (),
    ) -> None:
        specs[item_id] = {
            "claims": (),
            "conflict": None,
            "expected_status": "insufficient_context",
            "forbidden_answer_terms": list(dict.fromkeys(protected_terms)),
            "reference_answer": "I don't have enough cited context to answer this question.",
            "refusal_reason": reason,
            "temporal_comparison": None,
        }

    # Single-Chunk and exact-value cases.
    answer("single_chunk-success-01", (metric_claim("northstar", 2024, "revenue"),))
    answer("single_chunk-success-02", (metric_claim("meridian-retail", 2024, "margin"),))
    answer("single_chunk-success-03", (metric_claim("harbor-energy", 2023, "cash"),))
    answer("single_chunk-success-04", (offering_claim("atlas-cloud", (2023, 2024)),))
    answer("single_chunk-success-05", (segment_claim("atlas-logistics", (2023, 2024)),))
    answer("single_chunk-boundary-01", (metric_claim("northstar", 2024, "revenue"),))
    answer("single_chunk-boundary-02", (identity_claim("atlas-cloud", (2023, 2024)),))

    answer("exact_value-success-01", (metric_claim("northstar", 2024, "revenue"),))
    answer("exact_value-success-02", (metric_claim("meridian-retail", 2024, "margin"),))
    answer("exact_value-success-03", (metric_claim("harbor-energy", 2023, "cash"),))
    answer("exact_value-success-04", (metric_claim("atlas-cloud", 2024, "capital"),))
    answer("exact_value-success-05", (metric_claim("atlas-logistics", 2024, "revenue"),))
    answer("exact_value-boundary-01", (metric_claim("atlas-cloud", 2024, "revenue"),))
    answer("exact_value-boundary-02", (metric_claim("harbor-energy", 2024, "margin"),))

    # Cross-Chunk cases.
    answer("cross_chunk-success-01", (metric_claim("northstar", 2024, "revenue"), metric_claim("northstar", 2024, "margin")))
    answer("cross_chunk-success-02", (metric_claim("northstar", 2023, "revenue"), metric_claim("northstar", 2024, "revenue"), inference_claim("northstar", "revenue", "increased")))
    answer("cross_chunk-success-03", (offering_claim("atlas-cloud", (2023, 2024)), risk_claim("atlas-cloud", (2023, 2024), "risk-market")))
    answer("cross_chunk-success-04", (metric_claim("harbor-energy", 2024, "margin"), metric_claim("harbor-energy", 2024, "cash")))
    answer("cross_chunk-success-05", (segment_claim("atlas-cloud", (2024,)), segment_claim("atlas-logistics", (2024,))))
    answer("cross_chunk-boundary-01", (offering_claim("meridian-retail", (2024,)), metric_claim("meridian-retail", 2024, "cash")))
    answer("cross_chunk-boundary-02", (metric_claim("atlas-cloud", 2024, "revenue"), metric_claim("atlas-logistics", 2024, "revenue")))

    # Graph relationship cases.
    answer("graph_relationship-success-01", (offering_claim("northstar", (2023, 2024)),))
    answer("graph_relationship-success-02", (risk_claim("atlas-cloud", (2023, 2024), "risk-supply"),))
    answer("graph_relationship-success-03", (segment_claim("atlas-logistics", (2023, 2024)),))
    answer("graph_relationship-success-04", shared_risk_claims("atlas-cloud", "atlas-logistics", "risk-market"))
    answer("graph_relationship-success-05", shared_risk_claims("meridian-retail", "harbor-energy", "risk-supply"))
    answer("graph_relationship-boundary-01", (segment_claim("northstar", (2023, 2024)),))
    answer("graph_relationship-boundary-02", (offering_claim("atlas-cloud", (2023, 2024)), offering_claim("atlas-logistics", (2023, 2024))))

    # Compatible temporal comparisons require both periods plus labelled inference.
    temporal_specs = (
        ("temporal_conflict-success-01", "northstar", "revenue", "increased"),
        ("temporal_conflict-success-02", "atlas-cloud", "margin", "increased"),
        ("temporal_conflict-success-03", "atlas-logistics", "cash", "increased"),
        ("temporal_conflict-success-04", "meridian-retail", "revenue", "increased"),
        ("temporal_conflict-success-05", "harbor-energy", "revenue", "decreased"),
    )
    for item_id, company_key, metric, direction in temporal_specs:
        answer(
            item_id,
            (
                metric_claim(company_key, 2023, metric),
                metric_claim(company_key, 2024, metric),
                inference_claim(company_key, metric, direction),
            ),
            temporal_comparison={
                "inference_direction": direction,
                "must_label_inference": True,
                "required": True,
                "required_periods": ["fiscal year 2023", "fiscal year 2024"],
            },
        )
    answer(
        "temporal_conflict-boundary-01",
        (metric_claim("northstar", 2024, "revenue"),),
        forbidden_answer_terms=("$68.4 billion",),
    )
    answer(
        "temporal_conflict-boundary-02",
        (metric_claim("atlas-cloud", 2023, "margin"),),
        forbidden_answer_terms=("63.5%",),
    )

    # Unanswerable cases must refuse without inventing claims.
    for item_id in sorted(
        item_id for item_id in questions_by_id if item_id.startswith("unanswerable-")
    ):
        refuse(item_id, "unsupported")

    # Unauthorized cases refuse and may not echo protected facts or identities.
    refuse("unauthorized-success-01", "unauthorized", protected_terms=("$52.8 billion",))
    refuse("unauthorized-success-02", "unauthorized", protected_terms=("Atlas Fleet network",))
    refuse("unauthorized-success-03", "unauthorized", protected_terms=("$5.9 billion",))
    refuse("unauthorized-success-04", "unauthorized", protected_terms=("29.4%", "30.2%"))
    refuse("unauthorized-success-05", "unauthorized", protected_terms=("Atlas Cloud Services Ltd.", "Atlas Logistics Holdings Inc."))
    refuse("unauthorized-boundary-01", "unauthorized", protected_terms=("Northstar Systems plc", "NST"))
    refuse("unauthorized-boundary-02", "unauthorized", protected_terms=("$52.8 billion",))

    if set(specs) != set(questions_by_id):
        missing = sorted(set(questions_by_id) - set(specs))
        extra = sorted(set(specs) - set(questions_by_id))
        raise ValueError(f"answer gold coverage mismatch: missing={missing}, extra={extra}")

    records: list[dict[str, Any]] = []
    for item_id, question in sorted(questions_by_id.items()):
        spec = specs[item_id]
        claims: list[dict[str, Any]] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for index, raw_claim in enumerate(spec["claims"], start=1):
            item = dict(raw_claim)
            item["claim_id"] = f"{item_id}:claim-{index:02d}"
            claims.append(item)
            for source in evidence(item["evidence_chunk_keys"]):
                evidence_by_id[source["chunk_id"]] = source
        records.append(
            {
                "case_type": question["case_type"],
                "claims": claims,
                "conflict": spec["conflict"],
                "corpus_id": DATASET_ID,
                "corpus_version": DATASET_VERSION,
                "evidence": sorted(evidence_by_id.values(), key=lambda item: item["chunk_id"]),
                "expected_material_claim_count": len(claims),
                "expected_status": spec["expected_status"],
                "forbidden_answer_terms": spec["forbidden_answer_terms"],
                "gold_version": ANSWER_GOLD_VERSION,
                "id": item_id,
                "query": question["query"],
                "question_class": question["question_class"],
                "reference_answer": spec["reference_answer"],
                "refusal_reason": spec["refusal_reason"],
                "required_exact_tokens": list(
                    dict.fromkeys(
                        token for item in claims for token in item["exact_tokens"]
                    )
                ),
                "temporal_comparison": spec["temporal_comparison"],
            }
        )
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
    answers = list(
        _answer_gold_records(
            questions,
            chunks_by_key,
            documents,
            companies_by_key,
        )
    )
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
    answers.sort(key=lambda item: item["id"])
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
        "answers.jsonl": _jsonl_bytes(answers),
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
        "answer_gold": {
            "case_id_field": "id",
            "contains_predictions": False,
            "evidence_policy": "direct_claim_support",
            "evidence_unit": "chunk",
            "path": "answers.jsonl",
            "version": ANSWER_GOLD_VERSION,
        },
        "artifacts": [
            {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
            for path, payload in sorted(generated.items())
        ],
        "counts": {
            "active_chunks": len(chunks),
            "answer_annotations": len(answers),
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
        answers=tuple(answers),
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
    if counts.get("answer_annotations") != len(build.answers):
        errors.append("manifest answer annotation count does not match answer gold")

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
    documents_by_id = {item["document_id"]: item for item in build.documents}
    chunks_by_id = {item["chunk_id"]: item for item in build.chunks}
    if len(documents_by_key) != len(build.documents):
        errors.append("document keys must be unique")
    if len(documents_by_id) != len(build.documents):
        errors.append("document IDs must be unique")
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

    questions_by_id = {item["id"]: item for item in build.questions}
    answers_by_id = {item["id"]: item for item in build.answers}
    if len(answers_by_id) != len(build.answers) or set(answers_by_id) != set(questions_by_id):
        errors.append("answer gold must bind exactly once to every question ID")
    answer_gold_manifest = build.manifest.get("answer_gold", {})
    if answer_gold_manifest != {
        "case_id_field": "id",
        "contains_predictions": False,
        "evidence_policy": "direct_claim_support",
        "evidence_unit": "chunk",
        "path": "answers.jsonl",
        "version": ANSWER_GOLD_VERSION,
    }:
        errors.append("manifest answer-gold contract is invalid")
    for item_id, gold in answers_by_id.items():
        question = questions_by_id.get(item_id)
        if question is None:
            continue
        if gold.get("corpus_id") != DATASET_ID or gold.get("corpus_version") != DATASET_VERSION:
            errors.append(f"answer gold {item_id} has the wrong corpus identity")
        if gold.get("gold_version") != ANSWER_GOLD_VERSION:
            errors.append(f"answer gold {item_id} has the wrong gold version")
        if gold.get("query") != question["query"] or gold.get("question_class") != question["question_class"]:
            errors.append(f"answer gold {item_id} does not match its question")
        status = gold.get("expected_status")
        if status not in {"answered", "insufficient_context", "conflict"}:
            errors.append(f"answer gold {item_id} has an invalid expected status")
        refusal_case = question["question_class"] in {"unanswerable", "unauthorized"}
        if refusal_case != (status == "insufficient_context"):
            errors.append(f"answer gold {item_id} has inconsistent refusal behavior")
        claims = gold.get("claims")
        if not isinstance(claims, list):
            errors.append(f"answer gold {item_id} claims must be a list")
            continue
        if refusal_case and claims:
            errors.append(f"refusal answer gold {item_id} must not contain factual claims")
        if not refusal_case and not claims:
            errors.append(f"answer gold {item_id} requires material claims")
        if gold.get("expected_material_claim_count") != len(claims):
            errors.append(f"answer gold {item_id} claim count is inconsistent")
        positive_evidence = {
            chunk_id_value
            for chunk_id_value, grade in question["relevance"].items()
            if grade > 0
        }
        evidence_records = gold.get("evidence", [])
        if not isinstance(evidence_records, list):
            errors.append(f"answer gold {item_id} evidence must be a list")
            evidence_records = []
        declared_evidence: set[str] = set()
        for source in evidence_records:
            if not isinstance(source, dict):
                errors.append(f"answer gold {item_id} evidence must contain objects")
                continue
            source_chunk_id = source.get("chunk_id")
            if not isinstance(source_chunk_id, str) or source_chunk_id in declared_evidence:
                errors.append(f"answer gold {item_id} has invalid evidence IDs")
                continue
            declared_evidence.add(source_chunk_id)
            chunk = chunks_by_id.get(source_chunk_id)
            if chunk is None:
                errors.append(f"answer gold {item_id} references unknown evidence")
                continue
            document = documents_by_id.get(chunk["document_id"])
            if document is None:
                errors.append(f"answer gold {item_id} evidence has no document")
                continue
            expected_source = {
                "canonical_uri": document["canonical_uri"],
                "char_end": chunk["char_end"],
                "char_start": chunk["char_start"],
                "chunk_checksum": chunk["checksum"],
                "chunk_id": chunk["chunk_id"],
                "chunk_key": chunk["chunk_key"],
                "document_id": chunk["document_id"],
                "document_title": document["title"],
                "ordinal": chunk["ordinal"],
                "page_number": chunk["page_number"],
                "published_at": document["published_at"],
                "section": chunk["section"],
                "source_name": document["source_name"],
                "version_checksum": document["checksum"],
                "version_id": chunk["version_id"],
                "version_number": document["version_number"],
            }
            if source != expected_source:
                errors.append(
                    f"answer gold {item_id} evidence does not exactly bind its Chunk/Document/Version"
                )
        claim_ids: set[str] = set()
        canonical_claims: set[tuple[bool, str]] = set()
        used_claim_evidence: set[str] = set()
        for answer_claim in claims:
            claim_id_value = answer_claim.get("claim_id")
            if not isinstance(claim_id_value, str) or not claim_id_value or claim_id_value in claim_ids:
                errors.append(f"answer gold {item_id} has invalid claim IDs")
                continue
            claim_ids.add(claim_id_value)
            if answer_claim.get("material") is not True:
                errors.append(f"answer gold {item_id} claims must be material")
            reference_text = str(answer_claim.get("reference_text", ""))
            if not reference_text.strip():
                errors.append(f"answer gold {item_id} claim text must not be empty")
            if reference_text.startswith("Inference:"):
                errors.append(
                    f"answer gold {item_id} must keep inference labels out of claim text"
                )
            canonical_claim = (
                bool(answer_claim.get("inference")),
                reference_text,
            )
            if canonical_claim in canonical_claims:
                errors.append(
                    f"answer gold {item_id} repeats a canonical adjudicated claim"
                )
            canonical_claims.add(canonical_claim)
            rendered_reference = (
                f"Inference: {reference_text}"
                if answer_claim.get("inference")
                else reference_text
            )
            if rendered_reference not in gold.get("reference_answer", ""):
                errors.append(
                    f"answer gold {item_id} reference answer omits a rendered claim"
                )
            for term in answer_claim.get("required_terms", []):
                if str(term).casefold() not in reference_text.casefold():
                    errors.append(f"answer gold {item_id} required term is absent from its claim")
            for token in answer_claim.get("exact_tokens", []):
                if str(token) not in reference_text:
                    errors.append(f"answer gold {item_id} exact token is absent from its claim")
            claim_evidence = set(answer_claim.get("evidence_chunk_ids", []))
            used_claim_evidence.update(claim_evidence)
            if not claim_evidence or not claim_evidence <= chunk_ids:
                errors.append(f"answer gold {item_id} has invalid claim evidence")
            if not claim_evidence <= positive_evidence:
                errors.append(f"answer gold {item_id} uses evidence outside retrieval relevance")
            if not claim_evidence <= declared_evidence:
                errors.append(f"answer gold {item_id} omits claim evidence provenance")
            claim_tokens = _answer_content_tokens(reference_text)
            claim_sequence = _answer_content_sequence(reference_text)
            evidence_tokens: set[str] = set()
            for chunk_id_value in claim_evidence:
                chunk = chunks_by_id.get(chunk_id_value)
                if chunk is None:
                    continue
                source = source_texts.get(chunk["source_path"], "")
                evidence_text = source[chunk["char_start"] : chunk["char_end"]]
                evidence_tokens.update(_answer_content_tokens(evidence_text))
                if (
                    not answer_claim.get("inference")
                    and not _is_ordered_subsequence(
                        claim_sequence,
                        _answer_content_sequence(evidence_text),
                    )
                ):
                    errors.append(
                        f"answer gold {item_id} lists a Chunk that does not "
                        "directly support its sourced claim"
                    )
            allowed_tokens = (
                evidence_tokens | _ANSWER_INFERENCE_TERMS
                if answer_claim.get("inference")
                else evidence_tokens
            )
            missing_tokens = sorted(claim_tokens - allowed_tokens)
            if not claim_tokens or missing_tokens:
                claim_kind = (
                    "inference" if answer_claim.get("inference") else "sourced claim"
                )
                errors.append(
                    f"answer gold {item_id} {claim_kind} contains tokens absent "
                    f"from its evidence: {missing_tokens}"
                )
            comparison = answer_claim.get("comparison")
            if answer_claim.get("inference"):
                expected_comparison_fields = {
                    "direction",
                    "from_period",
                    "from_value",
                    "to_period",
                    "to_value",
                }
                if not isinstance(comparison, dict) or set(comparison) != expected_comparison_fields:
                    errors.append(
                        f"answer gold {item_id} inference lacks an auditable comparison"
                    )
                else:
                    from_parts = _answer_quantity_parts(str(comparison["from_value"]))
                    to_parts = _answer_quantity_parts(str(comparison["to_value"]))
                    if (
                        from_parts is None
                        or to_parts is None
                        or from_parts[1] != to_parts[1]
                    ):
                        errors.append(
                            f"answer gold {item_id} comparison quantities are incompatible"
                        )
                    else:
                        actual_direction = (
                            "increased"
                            if to_parts[0] > from_parts[0]
                            else "decreased"
                            if to_parts[0] < from_parts[0]
                            else "unchanged"
                        )
                        if comparison["direction"] != actual_direction:
                            errors.append(
                                f"answer gold {item_id} comparison direction "
                                "does not match its operands"
                            )
                    if comparison.get("direction") not in reference_text:
                        errors.append(
                            f"answer gold {item_id} comparison direction is absent from its claim"
                        )
                    for prefix in ("from", "to"):
                        period = str(comparison.get(f"{prefix}_period", ""))
                        value = str(comparison.get(f"{prefix}_value", ""))
                        if not any(
                            period in source_text and value in source_text
                            for chunk_id_value in claim_evidence
                            if (chunk := chunks_by_id.get(chunk_id_value)) is not None
                            and (
                                source_text := source_texts.get(
                                    chunk["source_path"], ""
                                )[
                                    chunk["char_start"] : chunk["char_end"]
                                ]
                            )
                        ):
                            errors.append(
                                f"answer gold {item_id} comparison operand lacks direct evidence"
                            )
            elif comparison is not None:
                errors.append(
                    f"answer gold {item_id} sourced claim cannot declare a comparison"
                )
        if declared_evidence != used_claim_evidence:
            errors.append(
                f"answer gold {item_id} top-level evidence must exactly equal claim evidence"
            )
        exact_tokens = list(
            dict.fromkeys(
                token for answer_claim in claims for token in answer_claim.get("exact_tokens", [])
            )
        )
        if gold.get("required_exact_tokens") != exact_tokens:
            errors.append(f"answer gold {item_id} exact-token index is inconsistent")
        if question["question_class"] == "exact_value" and not exact_tokens:
            errors.append(f"exact-value answer gold {item_id} requires exact tokens")
        if question["question_class"] == "unauthorized" and not gold.get("forbidden_answer_terms"):
            errors.append(f"unauthorized answer gold {item_id} requires protected terms")
        conflict = gold.get("conflict")
        if status == "conflict":
            if not isinstance(conflict, dict) or conflict.get("required") is not True:
                errors.append(f"conflict answer gold {item_id} requires a conflict contract")
        elif conflict is not None:
            errors.append(f"non-conflict answer gold {item_id} must not declare conflict")
        temporal = gold.get("temporal_comparison")
        if temporal is not None:
            if status != "answered" or not isinstance(temporal, dict):
                errors.append(f"temporal answer gold {item_id} must be answered")
            else:
                raw_periods = temporal.get("required_periods")
                periods = raw_periods if isinstance(raw_periods, list) else []
                if (
                    temporal.get("required") is not True
                    or temporal.get("must_label_inference") is not True
                    or not isinstance(raw_periods, list)
                    or len(periods) < 2
                    or not all(isinstance(period, str) and period for period in periods)
                    or not isinstance(temporal.get("inference_direction"), str)
                ):
                    errors.append(f"temporal answer gold {item_id} has an invalid contract")
                if not any(answer_claim.get("inference") for answer_claim in claims):
                    errors.append(f"temporal answer gold {item_id} requires labelled inference")
                direction = temporal.get("inference_direction")
                if not any(
                    answer_claim.get("inference")
                    and isinstance(answer_claim.get("comparison"), dict)
                    and answer_claim["comparison"].get("direction") == direction
                    for answer_claim in claims
                ):
                    errors.append(
                        f"temporal answer gold {item_id} direction does not match its inference"
                    )
                if any(period not in gold.get("reference_answer", "") for period in periods):
                    errors.append(f"temporal answer gold {item_id} omits a required period")

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
