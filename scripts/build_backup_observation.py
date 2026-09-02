#!/usr/bin/env python3
"""Verify restored graph identity and record one Stage 9 backup scenario."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def _state(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("sha256"), str)
        or value.get("schema_and_indexes_verified") is not True
    ):
        raise ValueError(f"invalid canonical graph state: {path}")
    return value


def _resource(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("passed") is not True
        or value.get("schema_version")
        != "production-container-resource-observation-v1"
    ):
        raise ValueError(f"invalid container resource observation: {path}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--restored-state", type=Path, required=True)
    parser.add_argument("--started-ns", type=int, required=True)
    parser.add_argument("--finished-ns", type=int, required=True)
    parser.add_argument("--dump-file", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--dump-command", required=True)
    parser.add_argument("--load-command", required=True)
    parser.add_argument("--source-container-resources", type=Path, required=True)
    parser.add_argument("--restored-container-resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = _state(args.source_state)
    restored = _state(args.restored_state)
    source_resources = _resource(args.source_container_resources)
    restored_resources = _resource(args.restored_container_resources)
    if args.started_ns <= 0 or args.finished_ns <= args.started_ns:
        raise ValueError("backup/restore timing must be positive and monotonic")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,62}", args.database) is None:
        raise ValueError("database name is invalid")
    expected_dump = (
        f"neo4j-admin database dump {args.database} "
        "--to-path=/backups --overwrite-destination=true"
    )
    expected_load = (
        f"neo4j-admin database load {args.database} "
        "--from-path=/backups --overwrite-destination=true"
    )
    if args.dump_command != expected_dump or args.load_command != expected_load:
        raise ValueError("backup commands do not match the reviewed workflow")
    if not args.dump_file.is_file():
        raise ValueError("backup artifact is missing")
    backup_size = args.dump_file.stat().st_size
    if backup_size <= 0:
        raise ValueError("backup artifact must be non-empty")
    source_resource_digest = _file_sha256(args.source_container_resources)
    restored_resource_digest = _file_sha256(args.restored_container_resources)
    resources_match = source_resources == restored_resources
    passed = source == restored and resources_match
    observation = {
        "backup_sha256": _file_sha256(args.dump_file),
        "backup_size_bytes": backup_size,
        "database": args.database,
        "container_resources_match": resources_match,
        "domain_status": None,
        "dump_command": args.dump_command,
        "error_code": None,
        "finished_ns": args.finished_ns,
        "http_status": 200,
        "latency_ms": (args.finished_ns - args.started_ns) / 1_000_000,
        "load_command": args.load_command,
        "passed": passed,
        "reason": None,
        "restored_business_node_count": restored.get("business_node_count"),
        "restored_business_relationship_count": restored.get(
            "business_relationship_count"
        ),
        "restored_state_sha256": restored["sha256"],
        "restored_container_resource_sha256": restored_resource_digest,
        "scenario_id": "backup_restore",
        "schema_and_indexes_verified": (
            source["schema_and_indexes_verified"] is True
            and restored["schema_and_indexes_verified"] is True
        ),
        "schema_version": "production-backup-restore-observation-v1",
        "source_business_node_count": source.get("business_node_count"),
        "source_business_relationship_count": source.get(
            "business_relationship_count"
        ),
        "source_state_sha256": source["sha256"],
        "source_container_resource_sha256": source_resource_digest,
        "started_ns": args.started_ns,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if not passed:
        print("restored canonical graph differs from the source graph")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
