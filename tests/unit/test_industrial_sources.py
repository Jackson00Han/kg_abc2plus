"""Offline catalog, integrity and bounded official-source acquisition tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from graphrag_prod.industrial.sources import (
    IndustrialSource,
    MAX_SOURCE_BYTES,
    PageRange,
    SourceAcquisitionError,
    SourceCatalogError,
    SourceIntegrityError,
    acquire_source,
    load_source_catalog,
)


CATALOG = Path(__file__).parents[2] / "datasets" / "industrial-v1" / "sources.json"
PDF = b"%PDF-1.7\nexample-pinned-manual\n%%EOF\n"


class _Frames(httpx.SyncByteStream):
    def __init__(self, *frames: bytes) -> None:
        self.frames = frames

    def __iter__(self):
        yield from self.frames


class IndustrialSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_source_catalog(CATALOG)
        self.source = replace(
            self.catalog.get("canalis-kt-installation"),
            sha256=hashlib.sha256(PDF).hexdigest(), byte_size=len(PDF),
        )

    def _client(self, handler) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(client.close)
        return client

    @staticmethod
    def _response(content: bytes = PDF, **headers: str) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=_Frames(content))

    def test_catalog_distinguishes_pins_portal_editions_and_applicability(self) -> None:
        self.assertEqual(len(self.catalog.sources), 8)
        self.assertEqual(sum(source.sha256 is not None for source in self.catalog.sources), 4)
        hvx = self.catalog.get("evopact-hvx-up-to-24kv")
        self.assertEqual(hvx.portal_version, "3.0")
        self.assertEqual(hvx.embedded_revision, "BQT34371-02")
        self.assertEqual(hvx.physical_pages, 108)
        self.assertGreater(hvx.byte_size, 60 * 1024 * 1024)
        china = self.catalog.get("evopact-hvx-china-12kv-maintenance")
        self.assertEqual(china.extraction_status, "OCR_REQUIRED")
        self.assertNotEqual(china.applicability, "CORE")
        for name in ("evopact-hvx-o-exclusion", "evopact-sureset-ansi-exclusion"):
            source = self.catalog.get(name)
            self.assertEqual(source.applicability, "EXCLUDED")
            self.assertIsNone(source.sha256)
            self.assertEqual(source.selected_page_ranges, ())

    def test_strict_metadata_rejects_types_unknown_fields_and_partial_pins(self) -> None:
        raw = json.loads(CATALOG.read_text())["sources"][0]
        for modification in (
            {"byte_size": True}, {"physical_pages": "84"},
            {"sha256": None}, {"byte_size": MAX_SOURCE_BYTES + 1},
            {"source_id": "../credentials"}, {"source_kind": []},
            {"language": "en"}, {"unexpected": "value"},
            {"portal_date": "20260906"}, {"filename": "../manual.pdf"},
            {"embedded_revision": None},
        ):
            with self.subTest(modification=modification), self.assertRaises(SourceCatalogError):
                IndustrialSource.from_mapping({**raw, **modification})

    def test_catalog_rejects_duplicate_json_fields_and_ids(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text('{"schema_version":"industrial-sources-v1","schema_version":"industrial-sources-v1"}')
            with self.assertRaises(SourceCatalogError):
                load_source_catalog(path)
            value = json.loads(CATALOG.read_text())
            value["sources"].append(value["sources"][0])
            path.write_text(json.dumps(value))
            with self.assertRaises(SourceCatalogError):
                load_source_catalog(path)

    def test_page_ranges_are_inspected_ordered_and_within_pinned_pdf(self) -> None:
        for selections in (
            (PageRange(85, 86, "outside source"),),
            (PageRange(4, 7, "one"), PageRange(7, 8, "overlap")),
        ):
            with self.subTest(selections=selections), self.assertRaises(SourceCatalogError):
                replace(self.source, selected_page_ranges=selections)
        with self.assertRaises(SourceCatalogError):
            PageRange(True, 2, "boolean is not page 1")

    def test_urls_reject_nonofficial_hosts_credentials_and_insecure_redirects(self) -> None:
        for url in (
            "http://download.se.com/files", "https://download.se.com.attacker.example/files",
            "https://user:secret@download.se.com/files", "https://127.0.0.1/files",
            "https://download.se.com:8443/files", "https://download.se.com/files#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(SourceCatalogError):
                replace(self.source, download_url=url)

    def test_valid_download_streams_to_atomic_cache_and_reuse_is_offline(self) -> None:
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(200, stream=_Frames(PDF[:3], PDF[3:]))
        client = self._client(handler)
        with TemporaryDirectory() as folder:
            path = acquire_source(self.source, Path(folder), client=client)
            self.assertEqual(path.read_bytes(), PDF)
            self.assertEqual(acquire_source(self.source, Path(folder), client=client), path)
            self.assertEqual(len(requests), 1)
            self.assertEqual([item.resolve() for item in Path(folder).iterdir()], [path])

    def test_changed_edition_or_corrupt_cache_is_never_overwritten(self) -> None:
        calls = []
        client = self._client(lambda request: calls.append(request) or self._response())
        with TemporaryDirectory() as folder:
            target = Path(folder) / f"{self.source.source_id}.pdf"
            target.write_bytes(b"old edition")
            with self.assertRaises(SourceIntegrityError):
                acquire_source(self.source, Path(folder), client=client)
            self.assertEqual(target.read_bytes(), b"old edition")
            self.assertEqual(calls, [])

    def test_mismatched_download_leaves_no_partial_or_final_file(self) -> None:
        for payload in (b"%PDF-wrong", PDF + b"excess", b"<html>not a PDF</html>"):
            with self.subTest(payload=payload), TemporaryDirectory() as folder:
                with self.assertRaises(SourceIntegrityError):
                    acquire_source(self.source, Path(folder), client=self._client(lambda _: self._response(payload)))
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_bad_advertised_length_and_encoding_fail_before_writing(self) -> None:
        for headers in ({"content-length": "999999999"}, {"content-length": "invalid"}, {"content-encoding": "gzip"}):
            with self.subTest(headers=headers), TemporaryDirectory() as folder:
                client = self._client(lambda _: httpx.Response(200, headers=headers, stream=_Frames(PDF)))
                with self.assertRaises(SourceAcquisitionError):
                    acquire_source(self.source, Path(folder), client=client)
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_redirects_are_validated_before_following(self) -> None:
        requests = []
        def denied(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://attacker.example/manual.pdf"})
        with TemporaryDirectory() as folder:
            with self.assertRaises(SourceAcquisitionError):
                acquire_source(self.source, Path(folder), client=self._client(denied))
            self.assertEqual(len(requests), 1)
        requests.clear()
        def allowed(request):
            requests.append(str(request.url))
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "/verified.pdf"})
            return self._response()
        with TemporaryDirectory() as folder:
            acquire_source(self.source, Path(folder), client=self._client(allowed))
            self.assertEqual(requests[-1], "https://download.se.com/verified.pdf")

    def test_redirect_loop_and_provider_timeout_are_bounded_and_clean(self) -> None:
        requests = []
        def loop(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "/loop"})
        with TemporaryDirectory() as folder:
            with self.assertRaises(SourceAcquisitionError):
                acquire_source(self.source, Path(folder), client=self._client(loop))
            self.assertEqual(len(requests), 6)
        def timeout(_):
            raise httpx.ReadTimeout("provider-specific failure")
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SourceAcquisitionError, "failed safely"):
                acquire_source(self.source, Path(folder), client=self._client(timeout))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cooperative_deadline_checks_small_frames_without_buffering(self) -> None:
        client = self._client(lambda _: httpx.Response(200, stream=_Frames(PDF[:1], PDF[1:])))
        with TemporaryDirectory() as folder, patch(
            "graphrag_prod.industrial.sources.time.monotonic", side_effect=[0.0, 0.0, 181.0],
        ):
            with self.assertRaisesRegex(SourceAcquisitionError, "deadline"):
                acquire_source(self.source, Path(folder), client=client)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_external_cache_and_regular_file_boundary(self) -> None:
        client = self._client(lambda _: self._response())
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(SourceAcquisitionError, "outside"):
                acquire_source(self.source, root / "cache", excluded_roots=(root,), client=client)
            outside = root / "target.pdf"
            outside.write_bytes(PDF)
            (root / f"{self.source.source_id}.pdf").symlink_to(outside)
            with self.assertRaises(SourceIntegrityError):
                acquire_source(self.source, root, client=client)

    def test_unknown_ids_and_unpinned_metadata_never_trigger_network(self) -> None:
        with self.assertRaises(SourceCatalogError):
            self.catalog.get("https://attacker.example/file.pdf")
        with TemporaryDirectory() as folder:
            client = self._client(lambda _: self.fail("unexpected network request"))
            with self.assertRaises(SourceAcquisitionError):
                acquire_source(self.catalog.get("canalis-kta-catalog"), Path(folder), client=client)


if __name__ == "__main__":
    unittest.main()
