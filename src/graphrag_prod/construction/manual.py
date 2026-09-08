"""Explicit human fact records, validated without an extraction provider.

The stored text is a rendering of fields the user explicitly submitted. It is
labelled as a human record everywhere; it is not evidence attributed to an
external document. Immutable source storage is reused for versioning and ACLs.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import unicodedata

from .extraction import OpenAICompatibleOntologyExtractor, ExtractionLimits
from graphrag_prod.knowledge.trust import KnowledgeOrigin

HUMAN_SOURCE_PREFIX = 'urn:graphrag:human:'
MANUAL_PROFILE_PREFIX = 'human-input:v1:'
MANUAL_PROMPT_VERSION = 'no-model:human-input:v1'


@dataclass(frozen=True, slots=True)
class PreparedManualRecord:
    text: str
    response_json: str
    signature: str


def prepare_manual_record(value: dict) -> PreparedManualRecord:
    """Accept one closed, explicitly typed entity, property or relationship."""
    allowed = {'kind', 'subject', 'predicate', 'object_entity', 'literal'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('unknown manual fact fields')
    kind = value.get('kind')
    if kind not in {'ENTITY', 'PROPERTY', 'RELATIONSHIP'}:
        raise ValueError('unknown manual fact kind')

    def text(raw, limit=256):
        if not isinstance(raw, str):
            raise ValueError('manual fields must be strings')
        result = unicodedata.normalize('NFC', raw).strip()
        if not result or len(result) > limit or any(ord(c) < 32 for c in result):
            raise ValueError('manual field is empty, oversized or contains controls')
        return result

    def entity(raw):
        if not isinstance(raw, dict) or set(raw) != {'entity_type', 'canonical_name'}:
            raise ValueError('manual entity needs exactly a type and name')
        return text(raw['entity_type'], 128), text(raw['canonical_name'])

    subject_type, subject_name = entity(value.get('subject'))
    statement = subject_name
    entities = [{'ref': 'subject', 'type': subject_type, 'mentions': [
        {'text': subject_name, 'start': 0, 'end': len(subject_name), 'confidence': 1.0}]}]
    relationships, properties = [], []
    predicate = value.get('predicate')
    target, literal = value.get('object_entity'), value.get('literal')
    if kind == 'ENTITY':
        if any(v is not None for v in (predicate, target, literal)):
            raise ValueError('entity input cannot contain fact fields')
    else:
        predicate = text(predicate, 128)
        statement += f' — {predicate} → '
        if kind == 'RELATIONSHIP':
            if literal is not None:
                raise ValueError('relationship cannot contain a literal')
            target_type, target_name = entity(target)
            start = len(statement)
            statement += target_name
            entities.append({'ref': 'object', 'type': target_type, 'mentions': [
                {'text': target_name, 'start': start, 'end': len(statement), 'confidence': 1.0}]})
            relationships.append({'type': predicate, 'source_ref': 'subject',
                                  'target_ref': 'object', 'confidence': 1.0, 'properties': []})
        else:
            if target is not None or not isinstance(literal, dict):
                raise ValueError('property needs a literal and no object entity')
            fields = {'raw_literal', 'raw_unit', 'raw_valid_from', 'raw_valid_to', 'raw_observed_at'}
            if set(literal) - fields or 'raw_literal' not in literal:
                raise ValueError('invalid manual literal fields')
            raw = text(literal['raw_literal'], 1024)
            statement += raw
            prop = {'entity_ref': 'subject', 'property': predicate, 'raw_literal': raw, 'confidence': 1.0}
            for input_key, output_key in [('raw_unit', 'unit'), ('raw_valid_from', 'valid_from'),
                                           ('raw_valid_to', 'valid_to'), ('raw_observed_at', 'observed_at')]:
                item = literal.get(input_key)
                prop[output_key] = None if item is None else text(item, 128)
                if item is not None:
                    statement += f' · {output_key}: {prop[output_key]}'
            properties.append(prop)
    if len(statement) > 1100:
        raise ValueError('manual record exceeds the single-record bound')
    for fact in [*relationships, *properties]:
        fact['evidence'] = {'text': statement, 'start': 0, 'end': len(statement)}
    response = json.dumps({'entities': entities, 'relationships': relationships,
                           'property_facts': properties}, ensure_ascii=False, sort_keys=True)
    signature = MANUAL_PROFILE_PREFIX + hashlib.sha256(response.encode()).hexdigest()
    return PreparedManualRecord(statement, response, signature)


class ManualRecordValidator:
    """Reuse strict ontology/value validation, with no model or remote client."""
    is_manual = True
    max_validation_attempts = 1
    prompt_version = MANUAL_PROMPT_VERSION
    model = 'human-input/no-model'
    limits = ExtractionLimits(timeout_seconds=1.0)

    def __init__(self, tbox, record: PreparedManualRecord):
        self.active_tbox = tbox
        self.record = record
        self.validator = OpenAICompatibleOntologyExtractor(
            client=object(), model=self.model, active_tbox=tbox,
            prompt_version=self.prompt_version, limits=self.limits,
        )

    def extract_audited(self, *, artifact_id, input_hash, chunk, profile):
        if chunk.text != self.record.text or chunk.char_start != 0:
            raise ValueError('manual input must retain its exact single-record content')
        result = self.validator.revalidate_saved_response(self.record.response_json, chunk=chunk, profile=profile)
        return replace(result, origin=KnowledgeOrigin.HUMAN_SUPPLEMENT)
