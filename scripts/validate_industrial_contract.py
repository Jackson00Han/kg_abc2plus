#!/usr/bin/env python3
"""Validate the versioned industrial development scope and acceptance contract."""

from pathlib import Path

from graphrag_prod.industrial.contract import load_industrial_contract


def main() -> None:
    contract = load_industrial_contract(
        Path(__file__).resolve().parents[1] / "contracts/industrial_knowledge.v1.json"
    )
    print(
        f"Industrial contract {contract['version']}: "
        f"{len(contract['question_classes'])} question classes; "
        "source, scope, evidence and resource boundaries valid; development-only"
    )


if __name__ == "__main__":
    main()
