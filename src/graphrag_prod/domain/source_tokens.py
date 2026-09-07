"""Exact source-token boundaries shared by extraction and downstream records.

The STRING exception concerns assigned Han adjacency only. It does not establish
word segmentation, negation, ownership, or factual entailment.
"""

from __future__ import annotations

import unicodedata


LITERAL_BOUNDARY_POLICY = "typed-string-cjk-adjacency-v1"


def is_cjk_ideograph(character: str) -> bool:
    """Assigned unified/compatibility ideographs, including Unicode extensions.

    Unicode character names avoid accepting reserved block code points. The
    Unicode database version is pinned in the extraction request policy.
    """
    return unicodedata.name(character, "").startswith((
        "CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-",
    ))


def contains_exact_token(
    evidence: str, token: str, *, allow_cjk_adjacency: bool = False,
) -> bool:
    """Require verbatim text with conservative script-aware adjacency checks.

    Callers may enable ideograph adjacency only for declared STRING raw
    literals. These scripts do not require spaces around a quoted value.
    Units, time, numbers, mixed codes and other scripts retain strict boundaries.
    This is neither word segmentation nor a check of negation or entailment.
    """
    if not token:
        return False
    ideograph_literal = allow_cjk_adjacency and all(
        is_cjk_ideograph(character) for character in token
    )

    start = 0
    while True:
        index = evidence.find(token, start)
        if index < 0:
            return False
        end = index + len(token)
        left_ok = (
            not token[0].isalnum()
            or index == 0
            or not (evidence[index - 1].isalnum() or evidence[index - 1] == "_")
            or (ideograph_literal and is_cjk_ideograph(evidence[index - 1]))
        )
        right_ok = (
            not token[-1].isalnum()
            or end == len(evidence)
            or not (evidence[end].isalnum() or evidence[end] == "_")
            or (ideograph_literal and is_cjk_ideograph(evidence[end]))
        )
        if left_ok and right_ok:
            return True
        start = index + 1
