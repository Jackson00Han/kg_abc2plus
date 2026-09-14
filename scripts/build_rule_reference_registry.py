"""Build the supplied rule document's reference-only directory, without its body."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

from graphrag_prod.ontology.source import validate_rule_reference_registry


def build(path: Path) -> dict:
    original = path.read_bytes()
    text = original.decode("utf-8")
    versions = re.findall(r"`document_version=([^`]+)`", text)
    profiles = re.findall(r"`profile_id=([^`]+)`", text)
    if len(set(versions)) != 1 or len(set(profiles)) != 1:
        raise ValueError("rule metadata must have one explicit document version/profile")
    clauses: dict[str, int] = {}
    active = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## 4. "):
            active = True
        if not active:
            continue
        heading = re.match(r"^### \d+\.\d+ ([A-Z]\d+)：", line)
        row = re.match(r"^\| ([A-Z]\d+) \|", line)
        if heading:
            clauses[heading[1]] = number
        elif row:
            clauses.setdefault(row[1], number)
    value = {"registry_id": "ai_power.busway.rule_references", "version": 1, "documents": [{"document": path.name, "document_version": versions[0], "profile_id": profiles[0], "sha256": hashlib.sha256(original).hexdigest(), "clauses": [{"id": key, "line": line} for key, line in sorted(clauses.items())]}]}
    return validate_rule_reference_registry(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    arguments.output.write_text(json.dumps(build(arguments.source), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
