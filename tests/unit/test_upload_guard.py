"""Upload lease ownership and visible-scope isolation without a database."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from graphrag_prod.construction.upload_guard import (
    Neo4jUploadGuard, UploadAlreadyRunning, UploadReservation,
    UploadReservationUnavailable, UPLOAD_LEASE_SECONDS, _scope,
)
from graphrag_prod.construction.workflow import MAX_CONSTRUCTION_DEADLINE_SECONDS
from tests.unit.test_upload_preflight import PRINCIPAL, PUMP
from graphrag_prod.domain.ids import content_checksum


CHECKSUM = content_checksum(PUMP)


class _Session:
    def __init__(self, work):
        self.work = work

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute_write(self, callback, *args):
        return self.work(callback, *args)


class UploadGuardTests(unittest.TestCase):
    def test_scope_is_tenant_content_visible_groups_and_requested_groups(self):
        first = _scope(PRINCIPAL, CHECKSUM, ["engineering"])["reservation_id"]
        self.assertEqual(first, _scope(PRINCIPAL, CHECKSUM, {"engineering"})["reservation_id"])
        self.assertEqual(first, _scope(replace(PRINCIPAL, principal_id="another-reviewer"),
                                      CHECKSUM, ["engineering"])["reservation_id"])
        changed = [replace(PRINCIPAL, tenant_id="another-pump-test"),
                   replace(PRINCIPAL, groups=frozenset({"engineering", "maintenance"}))]
        for principal in changed:
            self.assertNotEqual(first, _scope(principal, CHECKSUM, ["engineering"])["reservation_id"])
        broader = changed[1]
        self.assertNotEqual(_scope(broader, CHECKSUM, ["engineering"])["reservation_id"],
                            _scope(broader, CHECKSUM, ["maintenance"])["reservation_id"])
        self.assertNotEqual(first, _scope(PRINCIPAL, content_checksum(PUMP + "\n"),
                                         ["engineering"])["reservation_id"])

    def test_guard_releases_on_success_and_preserves_original_failure(self):
        class Guard(Neo4jUploadGuard):
            released = []
            fail_release = False

            def claim(self, *_):
                return UploadReservation("pump-guard", "reservation", "token")

            def release(self, reservation):
                self.released.append(reservation)
                if self.fail_release:
                    raise UploadReservationUnavailable()

        guard = Guard(None)
        with guard.hold(PRINCIPAL, CHECKSUM, ["engineering"]) as reservation:
            self.assertEqual(reservation.owner_token, "token")
        self.assertEqual(len(guard.released), 1)
        guard.fail_release = True
        with self.assertRaisesRegex(ValueError, "provider failure"):
            with guard.hold(PRINCIPAL, CHECKSUM, ["engineering"]):
                raise ValueError("provider failure")
        self.assertEqual(len(guard.released), 2)
        with self.assertRaises(UploadReservationUnavailable):
            with guard.hold(PRINCIPAL, CHECKSUM, ["engineering"]):
                pass

    def test_busy_claim_has_generic_error_and_does_not_wait_for_provider(self):
        parameters = []

        def write(_callback, values):
            parameters.append(values)
            return False

        guard = Neo4jUploadGuard(SimpleNamespace(session=lambda **_: _Session(write)))
        with self.assertRaisesRegex(UploadAlreadyRunning, "same upload is already processing"):
            guard.claim(PRINCIPAL, CHECKSUM, ["engineering"])
        self.assertEqual(parameters[0]["lease_seconds"], UPLOAD_LEASE_SECONDS)
        self.assertGreater(UPLOAD_LEASE_SECONDS, MAX_CONSTRUCTION_DEADLINE_SECONDS)
        self.assertFalse({"title", "text", "canonical_uri", "job_id"} & parameters[0].keys())

    def test_invalid_scope_is_rejected_before_database_work(self):
        guard = Neo4jUploadGuard(None)
        with self.assertRaises(PermissionError):
            guard.claim(PRINCIPAL, CHECKSUM, ["private"])
        with self.assertRaises(ValueError):
            guard.claim(PRINCIPAL, CHECKSUM, "engineering")
        with self.assertRaises(ValueError):
            guard.claim(PRINCIPAL, CHECKSUM.upper(), ["engineering"])
        with self.assertRaises(PermissionError):
            guard.claim(replace(PRINCIPAL, capabilities=frozenset()), CHECKSUM, ["engineering"])


if __name__ == "__main__":
    unittest.main()
