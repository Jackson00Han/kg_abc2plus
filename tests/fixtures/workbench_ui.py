"""Read the shipped Industrial markup and behavior for existing UI harnesses."""

import json
from pathlib import Path


STATIC = Path(__file__).resolve().parents[2] / "src/graphrag_prod/playground/static/industrial"


def governance_markup() -> str:
    declaration = (STATIC / "governance-template.mjs").read_text(encoding="utf-8")
    return json.loads(declaration.split("export default ", 1)[1].strip().removesuffix(";"))


def governance_source() -> str:
    """Expose actual code and parsed template without loading a retired page."""
    module = (STATIC / "governance.mjs").read_text(encoding="utf-8")
    return governance_markup() + "\n<script>\n" + module + "\n</script>"
