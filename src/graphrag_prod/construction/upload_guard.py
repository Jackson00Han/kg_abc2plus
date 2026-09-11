"""Short-transaction leases for concurrent uploads of the same visible content."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
from typing import Any, Iterator
from uuid import uuid4

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum

from .workflow import MAX_CONSTRUCTION_DEADLINE_SECONDS


# The workflow is cooperative: a provider/driver that disregards its timeout
# can outlive even its 900-second ceiling. This margin covers ordinary bounded
# cleanup, but is not fencing for a permanently stuck external provider. No
# database transaction stays open while parsing, embedding, or extracting.
UPLOAD_LEASE_SECONDS = int(MAX_CONSTRUCTION_DEADLINE_SECONDS) + 120


class UploadAlreadyRunning(RuntimeError):
    """An upload with the same content and access scope is still processing."""


class UploadReservationUnavailable(RuntimeError):
    """The upload reservation could not be verified or released."""


@dataclass(frozen=True, slots=True)
class UploadReservation:
    tenant_id: str
    reservation_id: str
    owner_token: str


_CLAIM = """
MERGE (reservation:UploadReservation {reservation_id: $reservation_id})
ON CREATE SET reservation.tenant_id = $tenant_id,
              reservation.content_checksum = $checksum,
              reservation.scope_checksum = $scope_checksum
SET reservation.__claim_lock = randomUUID()
WITH reservation
REMOVE reservation.__claim_lock
WITH reservation, datetime.realtime() AS now
WHERE reservation.tenant_id = $tenant_id
  AND reservation.content_checksum = $checksum
  AND reservation.scope_checksum = $scope_checksum
  AND (reservation.owner_token IS NULL OR reservation.owner_token = $owner_token
       OR reservation.lease_expires_at <= now)
SET reservation.owner_token = $owner_token,
    reservation.lease_expires_at = now + duration({seconds: $lease_seconds})
RETURN reservation.owner_token AS owner_token
"""

_RELEASE = """
MATCH (reservation:UploadReservation {
    tenant_id: $tenant_id, reservation_id: $reservation_id
})
SET reservation.__release_lock = randomUUID()
WITH reservation
REMOVE reservation.__release_lock
WITH reservation
WHERE reservation.owner_token = $owner_token
REMOVE reservation.owner_token, reservation.lease_expires_at
RETURN reservation.reservation_id AS reservation_id
"""


def _scope(principal: Principal, checksum: str, access_groups: Any) -> dict[str, Any]:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be an authenticated Principal")
    if "knowledge:construct" not in principal.capabilities:
        raise PermissionError("upload reservation requires knowledge:construct")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ValueError("upload checksum must be lowercase SHA-256")
    if not isinstance(access_groups, (set, frozenset, tuple, list)):
        raise ValueError("upload access groups must be an explicit collection")
    if (not access_groups or any(not isinstance(group, str) or not group.strip()
                                or group != group.strip() for group in access_groups)):
        raise ValueError("upload access groups must not be empty")
    groups = frozenset(access_groups)
    if not groups <= principal.groups:
        raise PermissionError("upload access groups exceed principal access")
    scope_checksum = content_checksum(json.dumps(
        {"visible_groups": sorted(principal.groups), "access_groups": sorted(groups)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ))
    reservation_id = content_checksum(json.dumps(
        ["upload-reservation-v1", principal.tenant_id, checksum, scope_checksum],
        separators=(",", ":"), ensure_ascii=False,
    ))
    return {"tenant_id": principal.tenant_id, "checksum": checksum,
            "scope_checksum": scope_checksum, "reservation_id": reservation_id}


class Neo4jUploadGuard:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver, self.database = driver, database
        self._claim_work = unit_of_work(timeout=5.0, metadata={
            "component": "upload-reservation", "operation": "claim",
        })(self._claim_tx)
        self._release_work = unit_of_work(timeout=5.0, metadata={
            "component": "upload-reservation", "operation": "release",
        })(self._release_tx)

    def claim(self, principal: Principal, checksum: str, access_groups: Any) -> UploadReservation:
        parameters = _scope(principal, checksum, access_groups)
        parameters.update(owner_token=uuid4().hex, lease_seconds=UPLOAD_LEASE_SECONDS)
        try:
            with self.driver.session(database=self.database) as session:
                claimed = session.execute_write(self._claim_work, parameters)
        except Exception as error:
            raise UploadReservationUnavailable("upload reservation is unavailable") from error
        if not claimed:
            # Do not expose another request's user, title, URI, task, or scope.
            raise UploadAlreadyRunning("the same upload is already processing")
        return UploadReservation(parameters["tenant_id"], parameters["reservation_id"],
                                 parameters["owner_token"])

    def release(self, reservation: UploadReservation) -> None:
        if not isinstance(reservation, UploadReservation):
            raise TypeError("release requires an upload reservation")
        try:
            with self.driver.session(database=self.database) as session:
                session.execute_write(self._release_work, reservation)
        except Exception as error:
            raise UploadReservationUnavailable("upload reservation release is unavailable") from error

    @contextmanager
    def hold(self, principal: Principal, checksum: str,
             access_groups: Any) -> Iterator[UploadReservation]:
        reservation = self.claim(principal, checksum, access_groups)
        failed = False
        try:
            yield reservation
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self.release(reservation)
            except UploadReservationUnavailable:
                # Preserve the original provider/construction failure. A failed
                # cleanup leaves a lease that expires; it cannot free an owner
                # that acquired the reservation after expiry.
                if not failed:
                    raise

    @staticmethod
    def _claim_tx(tx: Any, parameters: dict[str, Any]) -> bool:
        row = tx.run(_CLAIM, **parameters).single()
        return row is not None and row["owner_token"] == parameters["owner_token"]

    @staticmethod
    def _release_tx(tx: Any, reservation: UploadReservation) -> None:
        tx.run(_RELEASE, tenant_id=reservation.tenant_id,
               reservation_id=reservation.reservation_id,
               owner_token=reservation.owner_token).consume()
