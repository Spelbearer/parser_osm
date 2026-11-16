"""Cleanup of recognised disposable files, preserving inputs and current results."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from .runtime import read_json as read


def is_link(path):
    if path.is_symlink():
        return True
    try:
        return bool(
            getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except FileNotFoundError:
        return False


def remove_owned(path, root, protected=()):
    path, root = Path(path), Path(root).resolve()
    if not path.exists() and not is_link(path):
        return 0
    target = path.resolve()
    if target == root or not target.is_relative_to(root) or is_link(path):
        raise ValueError(f"Refusing cleanup outside owned output: {path}")
    for item in protected:
        item = Path(item).resolve()
        if item == target or item.is_relative_to(target):
            raise ValueError(f"Cleanup would remove protected input: {item}")
    size = 0
    if path.is_dir():
        for directory, dirs, files in os.walk(path, followlinks=False):
            if any(is_link(Path(directory) / name) for name in dirs + files):
                raise ValueError(f"Refusing cleanup of links/junctions: {path}")
            size += sum((Path(directory) / name).stat().st_size for name in files)
        shutil.rmtree(path)
    else:
        size = path.stat().st_size
        path.unlink()
    return size


def record_removal(path, root, protected, report, *, relative=False):
    try:
        report["bytes_removed"] += remove_owned(path, root, protected)
        report["removed"].append(str(path.relative_to(root) if relative else path))
    except (OSError, ValueError) as error:
        report["skipped"].append({"path": str(path), "reason": str(error)})


def cleanup_legacy(root, source):
    """Delete only recognised application artifacts after a valid current bundle exists."""
    root = Path(root)
    result = {"removed": [], "skipped": [], "bytes_removed": 0}
    skipped = result["skipped"]
    protected = [s["path"] for s in source.get("sources", [])]

    def delete(path):
        record_removal(path, root, protected, result, relative=True)

    for family in ["runs", "preview_runs", "teradata", "approved", "work"]:
        directory = root / family
        if not directory.is_dir() or is_link(directory):
            continue
        for item in list(directory.iterdir()):
            if not item.is_dir() or is_link(item):
                skipped.append({"path": str(item), "reason": "Unrecognised artifact"})
                continue
            known = False
            try:
                if family == "approved":
                    known = (
                        item.name.startswith("approved-")
                        and (item / "approval.json").is_file()
                        and read(item / "settings.json").get("boundaries_reviewed")
                        is True
                    )
                else:
                    info = read(item / "summary.json")
                    known = bool(
                        info.get("source_run")
                        or info.get("sources")
                        or info.get("run_id")
                    ) and info.get("status") in {
                        "SUCCESS",
                        "COLLECTION_PREVIEW_SUCCESS",
                        "COLLECTION_PREVIEW_INCOMPLETE",
                        "TERADATA_EXPORT_SUCCESS",
                        "RUNNING",
                        "FAILED",
                        "CANCELLED",
                    }
            except (OSError, ValueError):
                pass
            if known:
                delete(item)
            else:
                skipped.append({"path": str(item), "reason": "Unrecognised artifact"})
        try:
            directory.rmdir()
        except OSError:
            pass
    cache = root / "cache"
    if cache.is_dir() and not is_link(cache):
        for item in list(cache.iterdir()):
            if item.is_dir() and re.fullmatch(r"[0-9a-f]{64}", item.name):
                delete(item)
            else:
                skipped.append(
                    {"path": str(item), "reason": "Unrecognised cache entry"}
                )
        try:
            cache.rmdir()
        except OSError:
            pass
    kml = root / "kml"
    if (kml / "last_success.json").is_file():
        try:
            if read(kml / "last_success.json").get("status") == "KML_EXPORT_SUCCESS":
                allowed = all(
                    p.name in {"runs", "work", "last_success.json", "last_failure.json"}
                    or re.fullmatch(r"poi_layers-[0-9T-]+[a-f0-9]+\.zip", p.name)
                    for p in kml.iterdir()
                )
                if allowed:
                    delete(kml)
                else:
                    skipped.append(
                        {
                            "path": str(kml),
                            "reason": "Contains unrecognised files; preserved",
                        }
                    )
        except (OSError, ValueError):
            pass
    checks = root / "checks"
    if checks.is_dir() and not is_link(checks):
        for item in list(checks.iterdir()):
            known = False
            try:
                if item.is_file() and re.fullmatch(
                    r"(audit|boundary-diagnostics)-[0-9T-]+[a-f0-9]+\.json", item.name
                ):
                    doc = read(item)
                    known = "run" in doc
                elif item.is_dir() and item.name.startswith("boundary-recovery-"):
                    known = read(item / "report.json").get("status") in {
                        "RECOVERED_CANDIDATES",
                        "RECOVERY_INCOMPLETE",
                    }
            except (OSError, ValueError):
                pass
            if known:
                delete(item)
            else:
                skipped.append(
                    {"path": str(item), "reason": "Unrecognised diagnostic file"}
                )
        try:
            checks.rmdir()
        except OSError:
            pass
    # The lock itself is removed only after the legacy lock handle has closed.
    state = root / "state"
    if state.is_dir() and set(p.name for p in state.iterdir()) <= {
        "last_success.json",
        "process.lock",
    }:
        delete(state)
    return result


def cleanup_project(project, source):
    project = Path(project).resolve()
    result = {"removed": [], "skipped": [], "bytes_removed": 0}
    protected = [s["path"] for s in source.get("sources", [])]

    def delete(path):
        record_removal(path, project, protected, result)

    demo = project / "demo"
    try:
        if (demo / "settings.json").is_file():
            settings = read(demo / "settings.json")
            if (
                settings.get("coverage_id") == "demo"
                and settings.get("input", {}).get("path") == "example.osm.pbf"
                and (demo / "example.osm.pbf").stat().st_size < 1000000
            ):
                delete(demo)
    except (OSError, ValueError) as error:
        result["skipped"].append({"path": str(demo), "reason": str(error)})
    previews = project / "preview"
    if previews.is_dir() and not is_link(previews):
        for child in list(previews.iterdir()):
            try:
                document = read(child / "summary.json")
                if document.get("preview_only") is True and document.get("status") in {
                    "PREVIEW_SUCCESS",
                    "PREVIEW_INCOMPLETE",
                    "FAILED",
                    "CANCELLED",
                }:
                    delete(child)
                else:
                    result["skipped"].append(
                        {"path": str(child), "reason": "Not a completed owned preview"}
                    )
            except (OSError, ValueError):
                result["skipped"].append(
                    {"path": str(child), "reason": "Unrecognised preview artifact"}
                )
        try:
            previews.rmdir()
        except OSError:
            pass
    # Walk only source/development folders, never inputs, results or environments.
    for base in [project, project / "poi_osm", project / "tests", project / "scripts"]:
        if not base.is_dir() or is_link(base):
            continue
        for directory, dirs, files in os.walk(base, topdown=True, followlinks=False):
            here = Path(directory)
            for name in list(dirs):
                path = here / name
                if is_link(path):
                    dirs.remove(name)
                elif name == "__pycache__" or (
                    name in {".pytest_cache", ".ruff_cache"}
                    and (path / "CACHEDIR.TAG").is_file()
                ):
                    dirs.remove(name)
                    delete(path)
                elif here == project:
                    dirs.remove(name)
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    delete(here / name)
    return result
