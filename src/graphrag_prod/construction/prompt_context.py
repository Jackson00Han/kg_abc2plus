"""Lossless, request-only projections of ontology and source coordinates.

These are model input formats, never persisted ontology definitions or evidence.
All extraction candidates still pass the original T-Box and source validators.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from graphrag_prod.ontology.models import PropertyDefinition, TBoxVersion


PROMPT_CONTEXT_VERSION = "lossless-extraction-context:v1"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ontology_context(tbox: TBoxVersion) -> dict[str, Any]:
    """Intern identical property definitions without merging type-specific rules.

    Equality includes the name, datatype, unit, cardinality, description and
    constraints. Same-name properties with different rules get different refs.
    No type is selected by keywords, and no domain constraint is pruned.
    """
    definitions: dict[str, dict[str, Any]] = {}
    references: dict[str, str] = {}

    def property_refs(properties: Iterable[PropertyDefinition]) -> list[str]:
        result = []
        for definition in properties:
            value = definition.to_mapping()
            # Send constraints as JSON once, rather than JSON inside a string.
            if "constraints_json" in value:
                value["constraints"] = json.loads(value.pop("constraints_json"))
            signature = compact_json(value)
            if signature not in references:
                ref = f"p{len(references)}"
                references[signature] = ref
                definitions[ref] = value
            result.append(references[signature])
        return result

    entities = [
        {
            "name": item.name,
            "description": item.description,
            "identity_properties": list(item.identity_properties),
            "property_refs": property_refs(item.properties),
        }
        for item in tbox.entity_types if item.instance_allowed
    ]
    relationships = [
        {
            "name": item.name,
            "source_types": list(item.source_types),
            "allowed_type_pairs": [list(pair) for pair in item.allowed_type_pairs],
            "target_types": list(item.target_types),
            "source_cardinality": item.source_cardinality.value,
            "target_cardinality": item.target_cardinality.value,
            "property_refs": property_refs(item.properties),
            "description": item.description,
        }
        for item in tbox.relationship_types if item.instance_allowed
    ]
    return {
        "ontology_version_id": tbox.tbox_id,
        "ontology_checksum": tbox.checksum,
        "property_definitions": definitions,
        "entity_types": entities,
        "relationship_types": relationships,
    }


def token_span_context(text: str) -> dict[str, Any]:
    """Preserve every previous token coordinate in a compact row table."""
    return {
        "chunk_token_span_columns": ["start", "end", "text"],
        "chunk_token_spans": [
            [match.start(), match.end(), match.group()]
            for match in re.finditer(r"[A-Za-z0-9_]+|[^\s]", text)
        ],
    }
