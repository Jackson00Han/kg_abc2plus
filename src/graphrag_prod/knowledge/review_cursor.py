"""Bounded queue positions; cursors never grant access to records."""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime


def decode_review_cursor(cursor: str | None):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(value, list) or len(value) != 4 or value[0] != 1
                or not all(isinstance(item, str) for item in value[1:])):
            raise ValueError
        created_at = datetime.fromisoformat(value[1])
        if (created_at.utcoffset() is None or not 1 <= len(value[2]) <= 512
                or any(ord(char) < 32 for char in value[2])
                or value[3] not in {"ENTITY_MENTION", "ASSERTION"}):
            raise ValueError
        return created_at, value[2], value[3]
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as error:
        raise ValueError("invalid review queue cursor") from error


def encode_review_cursor(item) -> str:
    payload = [1, item.record.created_at.isoformat(), item.record.record_id,
               item.record_kind.value]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
