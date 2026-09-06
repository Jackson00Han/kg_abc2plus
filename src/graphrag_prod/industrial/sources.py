"""Strict official-source catalog and bounded, checksum-pinned acquisition.

Acquiring a manual does not approve its technical claims. Portal versions,
embedded editions and product applicability remain distinct catalog fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx


MAX_SOURCE_BYTES = 96 * 1024 * 1024
MAX_CATALOG_BYTES = 256 * 1024
MAX_DOWNLOAD_SECONDS = 180.0
MAX_READ_SECONDS = 20.0
MAX_REDIRECTS = 5
OFFICIAL_HOSTS = frozenset({
    "se.com", "www.se.com", "download.se.com",
    "schneider-electric.cn", "www.schneider-electric.cn",
    "download.schneider-electric.com",
})
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{2,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SourceCatalogError(ValueError):
    """Invalid or ambiguous catalog metadata."""


class SourceAcquisitionError(RuntimeError):
    """A bounded acquisition failed without replacing an existing source."""


class SourceIntegrityError(SourceAcquisitionError):
    """Source bytes differ from the immutable catalog pin."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SourceCatalogError(f"{name} must be non-empty trimmed text")
    if len(value) > 4_096 or any(ord(char) < 32 for char in value):
        raise SourceCatalogError(f"{name} contains invalid text")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise SourceCatalogError(f"{name} has missing or unexpected fields")


def _integer(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SourceCatalogError(f"{name} is outside its positive integer bound")
    return value


def _official_url(value: object) -> str:
    url = _text(value, "official URL")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https" and parts.hostname in OFFICIAL_HOSTS
            and parts.port in (None, 443) and not parts.username
            and not parts.password and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise SourceCatalogError("source URL must use an allowlisted official HTTPS host")
    return url


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise SourceCatalogError(f"{name} must be a bounded non-empty array")
    values = tuple(_text(item, name) for item in value)
    if len(set(values)) != len(values):
        raise SourceCatalogError(f"{name} must be unique")
    return values


@dataclass(frozen=True, slots=True)
class PageRange:
    start: int
    end: int
    purpose: str

    def __post_init__(self) -> None:
        _integer(self.start, "page start", 10_000)
        _integer(self.end, "page end", 10_000)
        if self.end < self.start:
            raise SourceCatalogError("page range must be ordered")
        _text(self.purpose, "page purpose")


@dataclass(frozen=True, slots=True)
class IndustrialSource:
    source_id: str
    title: str
    source_kind: str
    document_reference: str
    portal_url: str
    portal_date: str
    portal_version: str
    download_url: str
    filename: str
    embedded_revision: str | None
    embedded_date: str | None
    language: tuple[str, ...]
    equipment_class: str
    product_scope: tuple[str, ...]
    regional_scope: str
    applicability: str
    extraction_status: str
    sha256: str | None
    byte_size: int | None
    physical_pages: int | None
    selected_page_ranges: tuple[PageRange, ...]
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SOURCE_ID.fullmatch(_text(self.source_id, "source_id")):
            raise SourceCatalogError("source_id must be a safe catalog identifier")
        for name in (
            "title", "document_reference", "portal_version", "filename",
            "regional_scope",
        ):
            _text(getattr(self, name), name)
        for name in ("embedded_revision", "embedded_date"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if "/" in self.filename or "\\" in self.filename or not self.filename.lower().endswith(".pdf"):
            raise SourceCatalogError("filename must identify a PDF basename")
        _official_url(self.portal_url)
        _official_url(self.download_url)
        if not isinstance(self.portal_date, str):
            raise SourceCatalogError("portal_date must be an ISO date")
        try:
            parsed_date = date.fromisoformat(self.portal_date)
        except ValueError as error:
            raise SourceCatalogError("portal_date must be an ISO date") from error
        if parsed_date.isoformat() != self.portal_date:
            raise SourceCatalogError("portal_date must be an ISO date")
        choices = {
            "source_kind": {"OFFICIAL_MANUAL", "OFFICIAL_CATALOG"},
            "equipment_class": {"BUSBAR_TRUNKING", "VACUUM_CIRCUIT_BREAKER", "MEDIUM_VOLTAGE_SWITCHGEAR"},
            "applicability": {"CORE", "REFERENCE_ONLY", "EXCLUDED"},
            "extraction_status": {"TEXT_READY", "OCR_REQUIRED", "NOT_ACQUIRED"},
        }
        for name, allowed in choices.items():
            if not isinstance(getattr(self, name), str) or getattr(self, name) not in allowed:
                raise SourceCatalogError(f"{name} has an unsupported value")
        for name in ("language", "product_scope", "notes"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise SourceCatalogError(f"{name} must be a tuple")
            _strings(list(values), name)
        pins = (self.sha256, self.byte_size, self.physical_pages)
        if any(item is None for item in pins) != all(item is None for item in pins):
            raise SourceCatalogError("checksum, size and page count must be pinned together")
        if self.sha256 is not None:
            if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
                raise SourceCatalogError("sha256 must be a lowercase SHA-256 digest")
            _integer(self.byte_size, "byte_size", MAX_SOURCE_BYTES)
            _integer(self.physical_pages, "physical_pages", 10_000)
            if self.embedded_revision is None:
                raise SourceCatalogError("pinned source requires its embedded revision")
            if self.extraction_status == "NOT_ACQUIRED":
                raise SourceCatalogError("pinned source requires an inspected extraction status")
        elif self.extraction_status != "NOT_ACQUIRED":
            raise SourceCatalogError("unacquired source cannot claim inspected text")
        if not isinstance(self.selected_page_ranges, tuple) or len(self.selected_page_ranges) > 100:
            raise SourceCatalogError("selected_page_ranges must be a bounded tuple")
        previous = 0
        for page_range in self.selected_page_ranges:
            if not isinstance(page_range, PageRange):
                raise SourceCatalogError("page selection requires PageRange values")
            if self.physical_pages is None or page_range.end > self.physical_pages:
                raise SourceCatalogError("selected pages exceed the verified PDF")
            if page_range.start <= previous:
                raise SourceCatalogError("selected page ranges must be disjoint and ordered")
            previous = page_range.end

    @classmethod
    def from_mapping(cls, value: object) -> IndustrialSource:
        if not isinstance(value, dict):
            raise SourceCatalogError("source must be an object")
        _keys(value, set(cls.__dataclass_fields__), "source")
        fields = dict(value)
        for name in ("language", "product_scope", "notes"):
            fields[name] = _strings(fields[name], name)
        ranges = fields["selected_page_ranges"]
        if not isinstance(ranges, list):
            raise SourceCatalogError("selected_page_ranges must be an array")
        selections = []
        for item in ranges:
            if not isinstance(item, dict):
                raise SourceCatalogError("page range must be an object")
            _keys(item, {"start", "end", "purpose"}, "page range")
            selections.append(PageRange(**item))
        fields["selected_page_ranges"] = tuple(selections)
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    catalog_id: str
    version: str
    verified_at: str
    sources: tuple[IndustrialSource, ...]

    def __post_init__(self) -> None:
        if self.catalog_id != "schneider-industrial-sources" or self.version not in {"1.0.0", "1.0.1"}:
            raise SourceCatalogError("unsupported source catalog identity/version")
        if not isinstance(self.verified_at, str):
            raise SourceCatalogError("verified_at must be an ISO date")
        try:
            if date.fromisoformat(self.verified_at).isoformat() != self.verified_at:
                raise ValueError
        except ValueError as error:
            raise SourceCatalogError("verified_at must be an ISO date") from error
        if not isinstance(self.sources, tuple) or not 1 <= len(self.sources) <= 32:
            raise SourceCatalogError("catalog sources must be a bounded tuple")
        if any(not isinstance(item, IndustrialSource) for item in self.sources):
            raise SourceCatalogError("catalog sources require IndustrialSource records")
        if len({item.source_id for item in self.sources}) != len(self.sources):
            raise SourceCatalogError("catalog source IDs must be unique")

    def get(self, source_id: str) -> IndustrialSource:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise SourceCatalogError("unknown allowlisted source ID")


def load_source_catalog(path: Path) -> SourceCatalog:
    with Path(path).open("rb") as handle:
        payload = handle.read(MAX_CATALOG_BYTES + 1)
    if len(payload) > MAX_CATALOG_BYTES:
        raise SourceCatalogError("source catalog exceeds its byte limit")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError) as error:
        raise SourceCatalogError("source catalog is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SourceCatalogError("source catalog must be an object")
    _keys(value, {"schema_version", "catalog_id", "version", "verified_at", "sources"}, "catalog")
    if value["schema_version"] != "industrial-sources-v1" or not isinstance(value["sources"], list):
        raise SourceCatalogError("unsupported source catalog schema")
    return SourceCatalog(
        catalog_id=value["catalog_id"], version=value["version"],
        verified_at=value["verified_at"],
        sources=tuple(IndustrialSource.from_mapping(item) for item in value["sources"]),
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceCatalogError("catalog JSON contains a duplicate field")
        result[key] = value
    return result


def _verify_file(path: Path, source: IndustrialSource) -> None:
    if path.is_symlink() or not path.is_file():
        raise SourceIntegrityError("cached source must be a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(64 * 1024):
            size += len(block)
            if size > MAX_SOURCE_BYTES:
                raise SourceIntegrityError("cached source exceeds the acquisition cap")
            digest.update(block)
    if size != source.byte_size or digest.hexdigest() != source.sha256:
        raise SourceIntegrityError("cached source differs from pinned edition; existing file was preserved")


def acquire_source(
    source: IndustrialSource,
    cache_dir: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
    client: httpx.Client | None = None,
) -> Path:
    """Fetch one catalog record into an explicit external cache, never overwrite.

The CLI supplies the repository as an excluded root. ``client`` is injectable
    for deterministic offline tests; production uses verified TLS without proxy or
    credential environment inheritance. Metadata-only records cannot be fetched.
    The cooperative 180-second budget is checked on every incoming frame; an
    already-blocked read can take at most another 20 seconds before timing out.
"""
    if not isinstance(source, IndustrialSource) or source.sha256 is None:
        raise SourceAcquisitionError("source must have a verified catalog checksum before acquisition")
    cache = Path(cache_dir).expanduser().resolve()
    if any(cache.is_relative_to(Path(root).resolve()) for root in excluded_roots):
        raise SourceAcquisitionError("original PDFs require a cache outside the repository")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{source.source_id}.pdf"
    if target.exists() or target.is_symlink():
        _verify_file(target, source)
        return target
    own_client = client is None
    selected_client = client or httpx.Client(trust_env=False, follow_redirects=False)
    temporary: Path | None = None
    started = time.monotonic()
    try:
        url = source.download_url
        for redirects in range(MAX_REDIRECTS + 1):
            _official_url(url)
            remaining = MAX_DOWNLOAD_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise SourceAcquisitionError("source acquisition exceeded its deadline")
            with selected_client.stream(
                "GET", url, headers={"Accept": "application/pdf", "Accept-Encoding": "identity"},
                follow_redirects=False, timeout=httpx.Timeout(min(MAX_READ_SECONDS, remaining)),
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if redirects == MAX_REDIRECTS or "location" not in response.headers:
                        raise SourceAcquisitionError("source redirect limit or metadata is invalid")
                    url = _official_url(urljoin(url, response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise SourceAcquisitionError("official source download did not return success")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise SourceAcquisitionError("compressed source transfer is not supported")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        advertised = int(content_length)
                    except ValueError as error:
                        raise SourceAcquisitionError("source has invalid content length") from error
                    if advertised != source.byte_size or advertised > MAX_SOURCE_BYTES:
                        raise SourceIntegrityError("download size differs from pinned edition")
                descriptor, name = tempfile.mkstemp(prefix=f".{source.source_id}-", suffix=".partial", dir=cache)
                temporary = Path(name)
                digest = hashlib.sha256()
                size = 0
                prefix = bytearray()
                with os.fdopen(descriptor, "wb") as handle:
                    for block in response.iter_raw():
                        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise SourceAcquisitionError("source acquisition exceeded its deadline")
                        size += len(block)
                        if size > MAX_SOURCE_BYTES or size > source.byte_size:
                            raise SourceIntegrityError("download exceeds pinned size or acquisition cap")
                        if len(prefix) < 5:
                            prefix.extend(block[:5 - len(prefix)])
                        digest.update(block)
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
                if bytes(prefix) != b"%PDF-" or size != source.byte_size or digest.hexdigest() != source.sha256:
                    raise SourceIntegrityError("download does not match the pinned PDF edition")
                # A hard link atomically publishes the completed file and cannot
                # replace another writer's edition. Both paths share a filesystem.
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    _verify_file(target, source)
                return target
        raise SourceAcquisitionError("source redirect limit exceeded")
    except (httpx.HTTPError, SourceCatalogError) as error:
        raise SourceAcquisitionError("official source acquisition failed safely") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if own_client:
            selected_client.close()
