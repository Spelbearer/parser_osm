import json

import pytest
from helpers import confirm_collection, run_collection, write_json

from poi_osm.cleanup import cleanup_project, remove_owned
from poi_osm.product import execute, main
from poi_osm.runtime import process_lock


def test_project_cleanup_is_limited_to_disposable_source_caches(tmp_path):
    for folder in [".ruff_cache", ".pytest_cache", "poi_osm/nested/__pycache__"]:
        path = tmp_path / folder
        path.mkdir(parents=True)
        (path / "CACHEDIR.TAG").write_text("cache")
        (path / "temporary").write_bytes(b"disposable")
    files = [
        "result/.state/registry.sqlite",
        "boundaries/regions.geojson",
        "pbf/input.osm.pbf",
        ".venv/lib/__pycache__/keep.pyc",
        ".git/config",
        "config/telecom.json",
        "notes.txt",
        "tests/fixtures/private.txt",
    ]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    (tmp_path / "poi_osm/old.pyc").write_bytes(b"disposable")
    report = cleanup_project(
        tmp_path, {"sources": [{"path": str(tmp_path / "pbf/input.osm.pbf")}]}
    )
    assert not report["skipped"] and report["bytes_removed"] > 0
    assert not (tmp_path / ".ruff_cache").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / "poi_osm/nested/__pycache__").exists()
    assert not (tmp_path / "poi_osm/old.pyc").exists()
    assert all((tmp_path / name).read_bytes() == b"keep" for name in files)
    assert cleanup_project(tmp_path, {})["removed"] == []


def test_unrecognised_cache_and_protected_input_are_preserved(tmp_path):
    cache = tmp_path / ".ruff_cache"
    cache.mkdir()
    (cache / "personal.txt").write_text("keep")
    path = tmp_path / "poi_osm/__pycache__"
    path.mkdir(parents=True)
    source = path / "input.osm.pbf"
    source.write_bytes(b"keep")
    report = cleanup_project(tmp_path, {"sources": [{"path": str(source)}]})
    assert source.read_bytes() == b"keep" and (cache / "personal.txt").is_file()
    assert len(report["skipped"]) == 1


def test_cleanup_does_not_follow_links(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.pyc").write_bytes(b"keep")
    project = tmp_path / "project"
    (project / "poi_osm").mkdir(parents=True)
    try:
        (project / "poi_osm/linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    cleanup_project(project, {})
    assert (external / "keep.pyc").read_bytes() == b"keep"
    with pytest.raises(ValueError):
        remove_owned(project / "poi_osm/linked", project)


def test_clean_command_preserves_snapshot_and_never_runs_exports(
    collection, monkeypatch, tmp_path
):
    confirm_collection(collection)
    run_collection(collection, "--production")
    root = collection / "results"
    execute(
        root, "adopt", collection / "config/settings.json", collection / "profile.json"
    )
    result = root / "result"
    before = {
        str(path.relative_to(result)): path.read_bytes()
        for path in result.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr("poi_osm.product.REPO", collection)
    cache = collection / ".ruff_cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("cache")
    stale = root / ".work/old"
    stale.mkdir(parents=True)
    write_json(stale / "owner.json", {"root": str(root), "child_pid": None})
    (stale / "temporary.idx").write_bytes(b"temporary")
    private = root / "checks"
    private.mkdir()
    (private / "personal.json").write_text("keep")

    def forbidden(*args, **kwargs):
        raise AssertionError("Cleanup must not extract, render, or publish")

    for name in ["run_collection", "render_in_child", "publish"]:
        monkeypatch.setattr("poi_osm.product." + name, forbidden)
    machine = tmp_path / "machine.json"
    write_json(
        machine,
        {
            "base_dir": str(collection),
            "product_folder": "results",
            "source_mode": "download",
            "publication": {"enabled": True, "directory": str(tmp_path / "share")},
        },
    )
    assert main(["--local-config", str(machine), "--clean"]) == 0
    report = json.loads((root / "logs/cleanup.json").read_text())
    assert report["status"] == "PRODUCT_CLEAN_SUCCESS"
    assert report["cleanup"]["bytes_removed"] > 0
    assert any("personal.json" in item["path"] for item in report["cleanup"]["skipped"])
    assert not cache.exists() and not (root / ".work").exists()
    assert not (tmp_path / "share").exists()
    after = {
        str(path.relative_to(result)): path.read_bytes()
        for path in result.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert (collection / "inputs/alpha-261108.osm.pbf").is_file()
    with process_lock(root / ".product.lock"):
        with pytest.raises(RuntimeError, match="already running"):
            execute(root, "clean")


def test_clean_refuses_live_child_work(collection, monkeypatch):
    confirm_collection(collection)
    run_collection(collection, "--production")
    root = collection / "results"
    execute(
        root, "adopt", collection / "config/settings.json", collection / "profile.json"
    )
    stale = root / ".work/active"
    stale.mkdir(parents=True)
    write_json(stale / "owner.json", {"root": str(root), "child_pid": 123})
    monkeypatch.setattr("poi_osm.product.alive", lambda pid: True)
    with pytest.raises(RuntimeError, match="still running"):
        execute(root, "clean")
    assert (stale / "owner.json").is_file()
