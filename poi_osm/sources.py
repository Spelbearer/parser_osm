"""Verified, bounded-memory retrieval of explicitly scoped OSM snapshots.

Transport uses requests' inherited proxy and CA settings; certificate validation
is never disabled. A publisher checksum detects corrupt/incomplete downloads. It
does not replace HTTPS or establish the geographical completeness of a source:
that assertion must be supplied in the source configuration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


class SourceError(RuntimeError):
    """No complete, fresh and verifiable configured snapshot is usable."""


def _https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("Source and checksum URLs must be HTTPS without embedded credentials")
    return url


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or '<missing host>'}{parsed.path}"


def _error(exc: Exception) -> str:
    # requests exceptions may echo signed URLs. Never write their query strings
    # or embedded credentials into logs, reports, or persisted setup state.
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        status = f" HTTP {response.status_code}" if response is not None else ""
        return type(exc).__name__ + status
    return str(exc)


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    if response is not None and response.status_code in (429, 503):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    return min(2 ** attempt, 8)
            if delay > 60:
                raise SourceError("Publisher requested a longer Retry-After interval; retry the run later") from exc
            return max(0, delay)
    return min(2 ** attempt, 8)


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Follow only HTTPS redirects; reject a downgrade before sending a request."""
    for _ in range(6):
        response = session.request(method, _https(url), allow_redirects=False, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise SourceError("Publisher redirect lacks a Location header")
        url = _https(urljoin(url, location))
    raise SourceError("Too many publisher redirects")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError) as exc:
        raise SourceError(f"Invalid snapshot date: {value!r}") from exc


def _header_date(path: Path) -> date | None:
    try:
        import osmium

        with osmium.io.Reader(str(path)) as reader:
            value = reader.header().get("osmosis_replication_timestamp")
        return _date(value)
    except Exception as exc:
        raise SourceError(f"Cannot read OSM snapshot header: {path.name}: {exc}") from exc


def _snapshot_date(path: Path, config: dict, modified: str | None = None) -> str:
    declared = _date(config.get("source_date"))
    header = _header_date(path)
    if header and declared and header != declared:
        raise SourceError("Declared source_date disagrees with PBF replication timestamp")
    result = header or declared
    if result is None and modified:
        try:
            result = parsedate_to_datetime(modified).date()
        except (ValueError, TypeError, OverflowError) as exc:
            raise SourceError("Invalid publisher Last-Modified header") from exc
    if result is None:
        raise SourceError("Snapshot date missing: set source_date or use a dated PBF header")
    age_limit = config.get("max_age_days", 45)
    if not isinstance(age_limit, int) or isinstance(age_limit, bool) or age_limit < 0:
        raise SourceError("max_age_days must be a nonnegative integer")
    today = datetime.now(timezone.utc).date()
    if result > today + timedelta(days=1):
        raise SourceError("Snapshot timestamp is in the future")
    if (today - result).days > age_limit:
        raise SourceError(f"Snapshot {result.isoformat()} exceeds max_age_days={age_limit}")
    return result.isoformat()


def _scope(config: dict) -> tuple[str, bool]:
    coverage = config.get("coverage_id")
    if not isinstance(coverage, str) or not coverage.strip():
        raise SourceError("Every snapshot needs an explicit nonempty coverage_id")
    complete = config.get("complete", False)
    if not isinstance(complete, bool):
        raise SourceError("complete must be a JSON boolean")
    return coverage, complete


def _pin(config: dict) -> str | None:
    value = config.get("sha256")
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value)):
        raise SourceError("sha256 must contain exactly 64 hexadecimal characters")
    return value.lower() if value else None


def _local(config: dict) -> dict[str, Any]:
    path = Path(config["path"]).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise SourceError(f"Local snapshot missing or empty: {path}")
    coverage, complete = _scope(config)
    digest = _hash_file(path)
    expected = _pin(config)
    if expected and expected != digest:
        raise SourceError("Local snapshot SHA256 mismatch")
    return {
        "path": path,
        "source_date": _snapshot_date(path, config),
        "source_url": config.get("source_url", path.as_uri()),
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "coverage_id": coverage,
        "complete": complete,
    }


def _expected_digest(session: requests.Session, config: dict) -> tuple[str, str]:
    pin = _pin(config)
    if pin:
        return "sha256", pin
    checksum_url = config.get("checksum_url")
    if not checksum_url:
        raise SourceError("A remote snapshot requires sha256 or a publisher checksum_url")
    algorithm = config.get("checksum_algorithm", "md5").lower()
    lengths = {"md5": 32, "sha256": 64}
    if algorithm not in lengths:
        raise SourceError("checksum_algorithm must be md5 or sha256")
    with _request(session, "GET", checksum_url, stream=True, timeout=(15, config.get("timeout_seconds", 20))) as response:
        response.raise_for_status()
        _https(response.url)
        data = bytearray()
        for block in response.iter_content(4096):
            data.extend(block)
            if len(data) > 65536:
                raise SourceError("Publisher checksum response is unexpectedly large")
    checksum_text = data.decode("ascii", errors="strict").strip()
    match = re.fullmatch(r"([a-fA-F0-9]{" + str(lengths[algorithm]) + r"})(?:\s+\*?[^\r\n]+)?", checksum_text)
    if match is None:
        raise SourceError("Publisher checksum response is not a single valid digest")
    return algorithm, match.group(1).lower()


def _cache_paths(source_dir: Path, url: str) -> tuple[Path, Path]:
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return source_dir / f"{name}.osm.pbf", source_dir / f"{name}.json"


def _cached(config: dict, target: Path, metadata_path: Path) -> dict | None:
    if not target.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        coverage, complete = _scope(config)
        if metadata.get("source_url") != config["url"] or metadata.get("coverage_id") != coverage:
            return None
        digest = _hash_file(target)
        if digest != metadata["sha256"] or (config.get("sha256") and _pin(config) != digest):
            return None
        dated = dict(config)
        # Cached provenance is required even if the file lacks replication data.
        dated["source_date"] = config.get("source_date") or metadata["source_date"]
        source_date = _snapshot_date(target, dated)
        return {**metadata, "path": target, "source_date": source_date, "complete": complete,
                "size_bytes": target.stat().st_size}
    except (OSError, ValueError, KeyError, SourceError):
        return None


def _download(session: requests.Session, config: dict, source_dir: Path) -> dict:
    url = _https(config["url"])
    coverage, complete = _scope(config)
    algorithm, expected = _expected_digest(session, config)
    target, metadata_path = _cache_paths(source_dir, url)
    descriptor, temporary_name = tempfile.mkstemp(prefix="download-", suffix=".osm.pbf.part", dir=source_dir)
    temporary = Path(temporary_name)
    # File format selection in osmium cannot infer PBF from .part suffix.
    pbf_temporary = temporary.with_suffix("")
    os.close(descriptor)
    metadata_temp = metadata_path.with_suffix(".json.part")
    try:
        digest = hashlib.sha256()
        publisher_digest = hashlib.new(algorithm)
        size = 0
        next_progress = 64 * CHUNK_SIZE
        last_progress = time.monotonic()
        with _request(session, "GET", url, stream=True, timeout=(15, 120), headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            final_url = _https(response.url)
            declared_size = int(response.headers["Content-Length"]) if response.headers.get("Content-Length") else None
            if declared_size is not None and declared_size <= 0:
                raise SourceError("Publisher reports an empty snapshot")
            if declared_size and shutil.disk_usage(source_dir).free < declared_size + CHUNK_SIZE * 10:
                raise SourceError("Insufficient free disk for downloaded snapshot")
            with temporary.open("wb") as stream:
                for block in response.iter_content(CHUNK_SIZE):
                    if not block:
                        continue
                    size += len(block)
                    if config.get("max_size_bytes") and size > config["max_size_bytes"]:
                        raise SourceError("Snapshot exceeds configured max_size_bytes")
                    stream.write(block)
                    digest.update(block)
                    publisher_digest.update(block)
                    if not getattr(response, "progress_reported", False) and (size >= next_progress or time.monotonic() - last_progress >= 15):
                        LOGGER.info('DOWNLOAD %s: %.1f MiB%s', Path(urlsplit(url).path).name, size / CHUNK_SIZE,
                                    f' / {declared_size / CHUNK_SIZE:.1f} MiB' if declared_size else '')
                        next_progress = size + 64 * CHUNK_SIZE
                        last_progress = time.monotonic()
                stream.flush()
                os.fsync(stream.fileno())
            if not size or (declared_size is not None and size != declared_size):
                raise SourceError("Snapshot download is empty or truncated")
            modified = response.headers.get("Last-Modified")
        if publisher_digest.hexdigest() != expected:
            raise SourceError("Downloaded snapshot publisher checksum mismatch")
        os.replace(temporary, pbf_temporary)
        temporary = pbf_temporary
        metadata = {
            "source_date": _snapshot_date(temporary, config, modified),
            "source_url": url, "resolved_url": final_url,
            "sha256": digest.hexdigest(), "size_bytes": size,
            "coverage_id": coverage, "complete": complete,
            "checksum_algorithm": algorithm, "publisher_checksum": expected,
        }
        # Only the validated file replaces a previous cache. Interrupted metadata
        # replacement leaves a detectable hash mismatch, never a trusted cache.
        with metadata_temp.open("w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.replace(metadata_temp, metadata_path)
        return {**metadata, "path": target}
    finally:
        temporary.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)


def obtain_source(settings: dict, source_dir: Path, force: bool = False, *, session_factory=None) -> dict:
    """Return a verified fresh local snapshot, trying configured fallbacks.

    ``complete`` remains the explicit operator assertion from configuration;
    callers must refuse deletion/publication when it is false. Local manifests
    take precedence and are never silently replaced by a remote source.
    """
    if settings.get("input"):
        result = _local(settings["input"])
        if settings.get("coverage_id") and result["coverage_id"] != settings["coverage_id"]:
            raise SourceError("Local coverage_id differs from configured workflow scope")
        return result
    configs = settings.get("sources", [])
    if not configs:
        raise SourceError("No input snapshot or remote sources configured")
    source_dir = Path(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    expected_coverage = settings.get("coverage_id")
    with (session_factory or requests.Session)() as session:
        session.headers["User-Agent"] = "parser_osm/1.0 (OSM snapshot POI pipeline)"
        for config in configs:
            try:
                coverage, _ = _scope(config)
                if expected_coverage and coverage != expected_coverage:
                    raise SourceError("Source coverage_id differs from configured workflow scope")
                target, metadata = _cache_paths(source_dir, _https(config["url"]))
                if not force:
                    result = _cached(config, target, metadata)
                    if result:
                        algorithm, expected = _expected_digest(session, config)
                        if (algorithm == "sha256" and result["sha256"] == expected) or (
                            algorithm == result.get("checksum_algorithm") and expected == result.get("publisher_checksum")
                        ):
                            return result
                retries = config.get("retries", 3)
                if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 5:
                    raise SourceError("retries must be an integer from 1 to 5")
                for attempt in range(retries):
                    try:
                        return _download(session, config, source_dir)
                    except (OSError, ValueError, requests.RequestException, SourceError) as exc:
                        if attempt + 1 == retries:
                            raise
                        LOGGER.warning("Download failed (%s/%s): %s", attempt + 1, retries, _error(exc))
                        time.sleep(_retry_delay(exc, attempt))
            except (OSError, ValueError, KeyError, requests.RequestException, SourceError) as exc:
                failures.append(f"{_safe_url(config.get('url', ''))}: {_error(exc)}")
    raise SourceError("No usable source: " + "; ".join(failures))
