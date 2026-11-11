"""Source integrity tests using real tiny PBFs and deterministic HTTP doubles."""
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import osmium
import pytest
import requests

from poi_osm.sources import SourceError, obtain_source


def test_retry_after_is_respected_and_long_wait_defers():
    from poi_osm.sources import _retry_delay
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "7"
    error = requests.HTTPError(response=response)
    assert _retry_delay(error, 0) == 7
    response.headers["Retry-After"] = "120"
    with pytest.raises(SourceError, match="retry the run later"):
        _retry_delay(error, 0)
    response.headers["Retry-After"] = "invalid-header"
    assert _retry_delay(error, 1) == 2


def make_pbf(path: Path, snapshot_date: str | None = None) -> bytes:
    header = osmium.io.Header()
    if snapshot_date:
        header.set("osmosis_replication_timestamp", snapshot_date + "T00:00:00Z")
    with osmium.SimpleWriter(str(path), header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(37.6, 55.7), tags={"shop": "bakery"}))
    return path.read_bytes()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Response:
    def __init__(self, url, data=b"", status=200, headers=None):
        self.url, self.data, self.status_code = url, data, status
        self.headers = headers or {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


def remote_config(**overrides):
    return {"url": "https://example.org/current.osm.pbf", "checksum_url": "https://example.org/current.osm.pbf.md5",
            "coverage_id": "test-region", "complete": True, "retries": 1, **overrides}


def route(monkeypatch, pbf, *, digest=None, calls=None):
    def request(self, method, url, **kwargs):
        if calls is not None:
            calls.append((method, url))
        if url.endswith(".md5"):
            return Response(url, ((digest or hashlib.md5(pbf).hexdigest()) + "  current.osm.pbf\n").encode())
        return Response(url, pbf)
    monkeypatch.setattr(requests.Session, "request", request)


def test_local_header_and_manifest(tmp_path):
    pbf = tmp_path / "input.osm.pbf"
    data = make_pbf(pbf, today())
    result = obtain_source({"input": {"path": str(pbf), "coverage_id": "tiny", "sha256": hashlib.sha256(data).hexdigest()}}, tmp_path / "cache")
    assert result["source_date"] == today()
    assert result["complete"] is False
    assert not (tmp_path / "cache").exists()


def test_local_stale_and_mismatched_date_fail(tmp_path):
    pbf = tmp_path / "old.osm.pbf"
    old = (datetime.now(timezone.utc).date() - timedelta(days=60)).isoformat()
    make_pbf(pbf, old)
    config = {"path": str(pbf), "coverage_id": "tiny"}
    with pytest.raises(SourceError, match="max_age_days"):
        obtain_source({"input": config}, tmp_path)
    with pytest.raises(SourceError, match="disagrees"):
        obtain_source({"input": {**config, "source_date": today()}}, tmp_path)


def test_local_sha256_and_scope_mismatch(tmp_path):
    pbf = tmp_path / "input.osm.pbf"
    make_pbf(pbf, today())
    config = {"path": str(pbf), "coverage_id": "tiny"}
    with pytest.raises(SourceError, match="SHA256 mismatch"):
        obtain_source({"input": {**config, "sha256": "0" * 64}}, tmp_path)
    with pytest.raises(SourceError, match="coverage_id"):
        obtain_source({"input": config, "coverage_id": "different"}, tmp_path)


def test_header_without_date_requires_declared_date(tmp_path):
    pbf = tmp_path / "input.osm.pbf"
    make_pbf(pbf)
    config = {"path": str(pbf), "coverage_id": "tiny", "complete": True}
    with pytest.raises(SourceError, match="date missing"):
        obtain_source({"input": config}, tmp_path)
    assert obtain_source({"input": {**config, "source_date": today()}}, tmp_path)["complete"] is True


def test_download_and_reuse_verified_cache(monkeypatch, tmp_path):
    data = make_pbf(tmp_path / "fixture.osm.pbf", today())
    calls = []
    route(monkeypatch, data, calls=calls)
    source_dir = tmp_path / "cache"
    first = obtain_source({"sources": [remote_config()]}, source_dir)
    second = obtain_source({"sources": [remote_config()]}, source_dir)
    assert first["sha256"] == hashlib.sha256(data).hexdigest() == second["sha256"]
    assert first["complete"] and first["coverage_id"] == "test-region"
    assert len([u for _, u in calls if not u.endswith(".md5")]) == 1
    assert len([u for _, u in calls if u.endswith(".md5")]) == 2


def test_latest_checksum_change_refreshes_fresh_cache(monkeypatch, tmp_path):
    old = make_pbf(tmp_path / "old.osm.pbf", today())
    # Distinct valid PBF bytes, same source date.
    new_path = tmp_path / "new.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", today() + "T00:00:00Z")
    with osmium.SimpleWriter(str(new_path), header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=2, location=(37.7, 55.8)))
    new = new_path.read_bytes()
    route(monkeypatch, old)
    first = obtain_source({"sources": [remote_config()]}, tmp_path / "cache")
    route(monkeypatch, new)
    second = obtain_source({"sources": [remote_config()]}, tmp_path / "cache")
    assert first["sha256"] != second["sha256"]
    assert second["path"].read_bytes() == new


def test_bad_download_keeps_previous_cache(monkeypatch, tmp_path):
    data = make_pbf(tmp_path / "fixture.osm.pbf", today())
    route(monkeypatch, data)
    cache = tmp_path / "cache"
    first = obtain_source({"sources": [remote_config()]}, cache)
    route(monkeypatch, b"truncated PBF", digest="0" * 32)
    with pytest.raises(SourceError, match="checksum mismatch"):
        obtain_source({"sources": [remote_config()]}, cache, force=True)
    assert first["path"].read_bytes() == data
    assert not list(cache.glob("download-*.osm.pbf")), "failed partial files must be removed"


def test_fallback_and_no_unverified_remote(monkeypatch, tmp_path):
    data = make_pbf(tmp_path / "fixture.osm.pbf", today())
    route(monkeypatch, data)
    invalid = remote_config(url="https://example.org/no-checksum.osm.pbf", checksum_url=None)
    result = obtain_source({"sources": [invalid, remote_config()]}, tmp_path / "cache")
    assert result["source_url"].endswith("/current.osm.pbf")
    with pytest.raises(SourceError, match="requires sha256"):
        obtain_source({"sources": [invalid]}, tmp_path / "bad")


def test_https_redirect_downgrade_rejected_before_request(monkeypatch, tmp_path):
    calls = []
    def request(self, method, url, **kwargs):
        calls.append(url)
        return Response(url, status=302, headers={"Location": "http://example.org/insecure"})
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(SourceError, match="HTTPS"):
        obtain_source({"sources": [remote_config()]}, tmp_path)
    assert len(calls) == 1
