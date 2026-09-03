"""Neo4j persistence and atomic publication for property-graph T-Boxes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pint

from .models import TBoxStatus, TBoxVersion


_UNIT_REGISTRY = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)


class TBoxConflict(RuntimeError):
    """The requested T-Box write conflicts with durable or concurrent state."""


class TBoxValidationError(ValueError):
    """The requested T-Box contains a semantically invalid declaration."""


def _component_id(tbox_id: str, kind: str, *parts: str) -> str:
    payload = json.dumps(
        ["tbox-component-v1", tbox_id, kind, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checked_checksum(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("expected_checksum must be a hexadecimal SHA-256 digest")
    return normalized


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _validate_declared_units(value: TBoxVersion) -> None:
    for owner_kind, owner_name, definitions in (
        *(
            ("entity", item.name, item.properties)
            for item in value.entity_types
        ),
        *(
            ("relationship", item.name, item.properties)
            for item in value.relationship_types
        ),
    ):
        for definition in definitions:
            if definition.unit is None:
                continue
            try:
                _UNIT_REGISTRY.parse_units(definition.unit)
            except (pint.PintError, TypeError, ValueError) as exc:
                raise TBoxValidationError(
                    f"{owner_kind} property {owner_name}.{definition.name} "
                    f"declares an unrecognized Pint unit"
                ) from exc


def _version_record(value: TBoxVersion) -> dict[str, Any]:
    return {
        "tbox_id": value.tbox_id,
        "tenant_id": value.tenant_id,
        "key": value.key,
        "version": value.version,
        "checksum": value.checksum,
        "definition_json": json.dumps(
            value.with_status(TBoxStatus.DRAFT).to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _entity_rows(value: TBoxVersion) -> list[dict[str, Any]]:
    return [
        {
            "entity_type_id": _component_id(value.tbox_id, "entity", item.name),
            "tbox_id": value.tbox_id,
            "tenant_id": value.tenant_id,
            "name": item.name,
            "canonical_key_namespaces": list(item.canonical_key_namespaces),
            "identity_properties": list(item.identity_properties),
            "description": item.description,
        }
        for item in value.entity_types
    ]


def _relationship_rows(value: TBoxVersion) -> list[dict[str, Any]]:
    return [
        {
            "relationship_type_id": _component_id(
                value.tbox_id, "relationship", item.name
            ),
            "tbox_id": value.tbox_id,
            "tenant_id": value.tenant_id,
            "name": item.name,
            "source_types": list(item.source_types),
            "target_types": list(item.target_types),
            "source_cardinality": item.source_cardinality.value,
            "target_cardinality": item.target_cardinality.value,
            "description": item.description,
        }
        for item in value.relationship_types
    ]


def _property_rows(value: TBoxVersion) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    owners = (
        ("ENTITY", item.name, item.properties) for item in value.entity_types
    )
    relationship_owners = (
        ("RELATIONSHIP", item.name, item.properties)
        for item in value.relationship_types
    )
    for owner_kind, owner_name, properties in (*owners, *relationship_owners):
        owner_id = _component_id(
            value.tbox_id,
            "entity" if owner_kind == "ENTITY" else "relationship",
            owner_name,
        )
        for item in properties:
            rows.append(
                {
                    "property_definition_id": _component_id(
                        value.tbox_id,
                        "property",
                        owner_kind,
                        owner_name,
                        item.name,
                    ),
                    "tbox_id": value.tbox_id,
                    "tenant_id": value.tenant_id,
                    "owner_id": owner_id,
                    "owner_kind": owner_kind,
                    "owner_name": owner_name,
                    "name": item.name,
                    "datatype": item.datatype.value,
                    "required": item.required,
                    "cardinality": item.cardinality.value,
                    "unit": item.unit,
                    "description": item.description,
                }
            )
    return rows


def _decode_tbox(record: Any) -> TBoxVersion:
    data = dict(record)
    try:
        payload = json.loads(data["definition_json"])
        payload["status"] = data["status"]
        result = TBoxVersion.from_mapping(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TBoxConflict("stored T-Box payload is invalid") from exc
    if (
        result.tbox_id != data.get("tbox_id")
        or result.checksum != data.get("checksum")
        or result.tenant_id != data.get("tenant_id")
        or result.key != data.get("key")
        or result.version != data.get("version")
    ):
        raise TBoxConflict("stored T-Box identity or checksum is inconsistent")
    return result


class Neo4jTBoxStore:
    """Store T-Box versions and atomically switch one active version per key."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def import_version(
        self,
        value: TBoxVersion,
        *,
        expected_checksum: str | None = None,
    ) -> TBoxVersion:
        """Create or CAS-update a draft; exact replays are idempotent."""
        if not isinstance(value, TBoxVersion):
            raise TypeError("value must be a TBoxVersion")
        if value.status is not TBoxStatus.DRAFT:
            raise ValueError("only DRAFT T-Box versions can be imported")
        _validate_declared_units(value)
        expected_checksum = _checked_checksum(expected_checksum)
        now = datetime.now(UTC)
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._import_tx,
                value,
                expected_checksum,
                now,
            )
        return self.get(value.tenant_id, value.tbox_id)

    # Explicit alias for callers that name the operation after its payload.
    import_tbox = import_version

    def get(self, tenant_id: str, tbox_id: str) -> TBoxVersion:
        tenant_id = _required(tenant_id, "tenant_id")
        tbox_id = _required(tbox_id, "tbox_id")
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                MATCH (version:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id
                })
                RETURN version{.*} AS version
                """,
                tenant_id=tenant_id,
                tbox_id=tbox_id,
            ).single()
        if record is None:
            raise KeyError("unknown T-Box version")
        return _decode_tbox(record["version"])

    get_version = get

    def list(
        self,
        tenant_id: str,
        *,
        key: str | None = None,
        status: TBoxStatus | str | None = None,
    ) -> tuple[TBoxVersion, ...]:
        tenant_id = _required(tenant_id, "tenant_id")
        key = None if key is None else _required(key, "T-Box key")
        if status is not None and not isinstance(status, TBoxStatus):
            try:
                status = TBoxStatus(_required(status, "T-Box status").upper())
            except ValueError as exc:
                raise ValueError("unknown T-Box status") from exc
        with self.driver.session(database=self.database) as session:
            records = session.run(
                """
                MATCH (version:TBoxVersion {tenant_id: $tenant_id})
                WHERE ($key IS NULL OR version.key = $key)
                  AND ($status IS NULL OR version.status = $status)
                RETURN version{.*} AS version
                ORDER BY version.key, version.version
                """,
                tenant_id=tenant_id,
                key=key,
                status=None if status is None else status.value,
            )
            return tuple(_decode_tbox(record["version"]) for record in records)

    list_versions = list

    def active(self, tenant_id: str, key: str) -> TBoxVersion | None:
        tenant_id = _required(tenant_id, "tenant_id")
        key = _required(key, "T-Box key")
        with self.driver.session(database=self.database) as session:
            records = list(
                session.run(
                    """
                    MATCH (catalog:TBoxCatalog {
                        tenant_id: $tenant_id,
                        key: $key
                    })-[:ACTIVE_TBOX_VERSION]->(version:TBoxVersion {
                        tenant_id: $tenant_id,
                        key: $key
                    })
                    RETURN version{.*} AS version
                    LIMIT 2
                    """,
                    tenant_id=tenant_id,
                    key=key,
                )
            )
        if not records:
            return None
        if len(records) != 1:
            raise TBoxConflict("T-Box catalog has multiple active versions")
        result = _decode_tbox(records[0]["version"])
        if result.status is not TBoxStatus.PUBLISHED:
            raise TBoxConflict("active T-Box version is not published")
        return result

    active_version = active

    def publish(
        self,
        tenant_id: str,
        tbox_id: str,
        *,
        expected_active_tbox_id: str | None,
    ) -> TBoxVersion:
        tenant_id = _required(tenant_id, "tenant_id")
        tbox_id = _required(tbox_id, "tbox_id")
        if expected_active_tbox_id is not None:
            expected_active_tbox_id = _required(
                expected_active_tbox_id, "expected_active_tbox_id"
            )
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._publish_tx,
                tenant_id,
                tbox_id,
                expected_active_tbox_id,
                datetime.now(UTC),
            )
        return self.get(tenant_id, tbox_id)

    @staticmethod
    def _import_tx(
        tx: Any,
        value: TBoxVersion,
        expected_checksum: str | None,
        now: datetime,
    ) -> None:
        catalog = tx.run(
            """
            MERGE (catalog:TBoxCatalog {
                tenant_id: $tenant_id,
                key: $key
            })
            ON CREATE SET catalog.created_at = $now,
                          catalog.revision = 0
            SET catalog.__tbox_write_lock = randomUUID()
            WITH catalog
            REMOVE catalog.__tbox_write_lock
            RETURN catalog.revision AS revision
            """,
            tenant_id=value.tenant_id,
            key=value.key,
            now=now,
        ).single()
        if catalog is None:
            raise TBoxConflict("could not lock T-Box catalog")
        existing = tx.run(
            """
            OPTIONAL MATCH (version:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id
            })
            RETURN version{.*} AS version
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
        ).single()["version"]

        creating = existing is None
        if not creating:
            current = dict(existing)
            current_checksum = current.get("checksum")
            if current_checksum == value.checksum:
                return
            if current.get("status") != TBoxStatus.DRAFT.value:
                raise TBoxConflict("published or retired T-Box versions are immutable")
            if expected_checksum != current_checksum:
                raise TBoxConflict("draft T-Box checksum CAS failed")
            tx.run(
                """
                MATCH (version:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id
                })-[:DECLARES_ENTITY_TYPE|DECLARES_RELATIONSHIP_TYPE]->(component)
                OPTIONAL MATCH (component)-[:DECLARES_PROPERTY]->(property)
                WITH collect(DISTINCT property) + collect(DISTINCT component) AS nodes
                UNWIND nodes AS node
                DETACH DELETE node
                """,
                tenant_id=value.tenant_id,
                tbox_id=value.tbox_id,
            ).consume()
        elif expected_checksum is not None:
            raise TBoxConflict("draft T-Box checksum CAS failed")

        properties = _version_record(value)
        if creating:
            created = tx.run(
                """
                MATCH (catalog:TBoxCatalog {
                    tenant_id: $tenant_id,
                    key: $key
                })
                CREATE (version:TBoxVersion)
                SET version = $properties,
                    version.status = 'DRAFT',
                    version.created_at = $now,
                    version.updated_at = $now
                CREATE (catalog)-[:HAS_TBOX_VERSION]->(version)
                RETURN version.tbox_id AS tbox_id
                """,
                tenant_id=value.tenant_id,
                key=value.key,
                properties=properties,
                now=now,
            ).single()
            if created is None:
                raise TBoxConflict("could not create T-Box version")
        else:
            updated = tx.run(
                """
                MATCH (version:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id,
                    status: 'DRAFT'
                })
                SET version.checksum = $checksum,
                    version.definition_json = $definition_json,
                    version.updated_at = $now
                RETURN version.tbox_id AS tbox_id
                """,
                tenant_id=value.tenant_id,
                tbox_id=value.tbox_id,
                checksum=value.checksum,
                definition_json=properties["definition_json"],
                now=now,
            ).single()
            if updated is None:
                raise TBoxConflict("draft T-Box changed during import")

        Neo4jTBoxStore._write_definition_tx(tx, value)
        tx.run(
            """
            MATCH (catalog:TBoxCatalog {
                tenant_id: $tenant_id,
                key: $key
            })
            SET catalog.revision = catalog.revision + 1,
                catalog.updated_at = $now
            """,
            tenant_id=value.tenant_id,
            key=value.key,
            now=now,
        ).consume()

    @staticmethod
    def _write_definition_tx(tx: Any, value: TBoxVersion) -> None:
        tx.run(
            """
            MATCH (version:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id
            })
            UNWIND $rows AS row
            CREATE (entity_type:TBoxEntityType)
            SET entity_type = row
            CREATE (version)-[:DECLARES_ENTITY_TYPE]->(entity_type)
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
            rows=_entity_rows(value),
        ).consume()
        tx.run(
            """
            MATCH (version:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id
            })
            UNWIND $rows AS row
            CREATE (relationship_type:TBoxRelationshipType)
            SET relationship_type = row
            CREATE (version)-[:DECLARES_RELATIONSHIP_TYPE]->(relationship_type)
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
            rows=_relationship_rows(value),
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (owner {tenant_id: $tenant_id, tbox_id: $tbox_id})
            WHERE (row.owner_kind = 'ENTITY' AND owner:TBoxEntityType)
               OR (row.owner_kind = 'RELATIONSHIP' AND owner:TBoxRelationshipType)
            WITH owner, row
            WHERE owner[CASE
                WHEN row.owner_kind = 'ENTITY' THEN 'entity_type_id'
                ELSE 'relationship_type_id'
            END] = row.owner_id
            CREATE (property:TBoxPropertyDefinition)
            SET property = row
            CREATE (owner)-[:DECLARES_PROPERTY]->(property)
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
            rows=_property_rows(value),
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (relationship_type:TBoxRelationshipType {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                relationship_type_id: row.relationship_type_id
            })
            UNWIND row.source_types AS source_name
            MATCH (source:TBoxEntityType {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                name: source_name
            })
            CREATE (relationship_type)-[:ALLOWED_SOURCE_TYPE]->(source)
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
            rows=_relationship_rows(value),
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (relationship_type:TBoxRelationshipType {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                relationship_type_id: row.relationship_type_id
            })
            UNWIND row.target_types AS target_name
            MATCH (target:TBoxEntityType {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                name: target_name
            })
            CREATE (relationship_type)-[:ALLOWED_TARGET_TYPE]->(target)
            """,
            tenant_id=value.tenant_id,
            tbox_id=value.tbox_id,
            rows=_relationship_rows(value),
        ).consume()

    @staticmethod
    def _publish_tx(
        tx: Any,
        tenant_id: str,
        tbox_id: str,
        expected_active_tbox_id: str | None,
        now: datetime,
    ) -> None:
        record = tx.run(
            """
            MATCH (target:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id
            })
            MATCH (catalog:TBoxCatalog {
                tenant_id: $tenant_id,
                key: target.key
            })
            SET catalog.__tbox_publish_lock = randomUUID()
            WITH target, catalog
            REMOVE catalog.__tbox_publish_lock
            WITH target, catalog
            OPTIONAL MATCH (catalog)-[:ACTIVE_TBOX_VERSION]->(
                active:TBoxVersion
            )
            RETURN target{.*} AS target,
                   collect(active{.tbox_id, .version, .status}) AS active
            """,
            tenant_id=tenant_id,
            tbox_id=tbox_id,
        ).single()
        if record is None:
            raise KeyError("unknown T-Box version")
        target = dict(record["target"])
        active = [dict(item) for item in record["active"] if item]
        if len(active) > 1:
            raise TBoxConflict("T-Box catalog has multiple active versions")
        current = None if not active else active[0]
        current_id = None if current is None else current["tbox_id"]

        if current_id == tbox_id:
            if expected_active_tbox_id not in {None, tbox_id}:
                raise TBoxConflict("active T-Box version CAS failed")
            if target.get("status") != TBoxStatus.PUBLISHED.value:
                raise TBoxConflict("active T-Box version is not published")
            return
        if current_id != expected_active_tbox_id:
            raise TBoxConflict("active T-Box version CAS failed")
        if target.get("status") != TBoxStatus.DRAFT.value:
            raise TBoxConflict("only a DRAFT T-Box version can be published")
        if current is not None and int(target["version"]) <= int(current["version"]):
            raise TBoxConflict("T-Box publication cannot roll back its version")

        result = tx.run(
            """
            MATCH (catalog:TBoxCatalog {
                tenant_id: $tenant_id,
                key: $key
            })
            MATCH (target:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                status: 'DRAFT'
            })
            OPTIONAL MATCH (catalog)-[old_pointer:ACTIVE_TBOX_VERSION]->(
                old:TBoxVersion
            )
            DELETE old_pointer
            WITH DISTINCT catalog, target, old
            CREATE (catalog)-[:ACTIVE_TBOX_VERSION]->(target)
            SET target.status = 'PUBLISHED',
                target.published_at = $now,
                target.updated_at = $now,
                catalog.revision = catalog.revision + 1,
                catalog.updated_at = $now
            FOREACH (_ IN CASE
                WHEN old IS NOT NULL AND old <> target THEN [1]
                ELSE []
            END |
                SET old.status = 'RETIRED',
                    old.retired_at = $now,
                    old.updated_at = $now
            )
            RETURN target.tbox_id AS tbox_id
            """,
            tenant_id=tenant_id,
            key=target["key"],
            tbox_id=tbox_id,
            now=now,
        ).single()
        if result is None:
            raise TBoxConflict("T-Box changed during publication")
