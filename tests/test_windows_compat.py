"""Exercise sharing violations and non-UTF8 pipe defaults on any platform."""
import json
import os
from pathlib import Path

import pytest

from poi_osm.runtime import cleanup_index
from test_pipeline import project, invoke, manifest


def test_shared_index_does_not_block_publication_or_next_run(project, tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text('''
from pathlib import Path
import os
original = Path.unlink
def unlink(self, *args, **kwargs):
    released = os.environ.get('OSM_TEST_RELEASED_INDEX_NAMES', '').split(',')
    if self.suffix == '.idx' and self.name not in released:
        error = PermissionError('simulated Windows sharing violation')
        error.winerror = 32
        raise error
    return original(self, *args, **kwargs)
Path.unlink = unlink
''', encoding="utf-8")
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(hooks), str(repo)]))
    monkeypatch.setenv("PYTHONIOENCODING", "cp1251")
    result = invoke(project)
    assert "cleanup deferred" in result.stderr
    assert json.loads(result.stdout)["metrics"]["total"] == 4
    published = Path(manifest(project)["output"])
    assert not list(published.glob("*.idx"))
    old_indexes = set((project / "work/temporary/indexes").glob("*.idx"))
    assert len(old_indexes) == 1
    # Model Windows on Linux too: the first process's handle is now released,
    # but the next process can retain its own newly created index until exit.
    monkeypatch.setenv("OSM_TEST_RELEASED_INDEX_NAMES", ",".join(p.name for p in old_indexes))
    second = invoke(project)
    assert manifest(project)["metrics"]["changes"] == {"UNCHANGED": 4}
    assert all(not path.exists() for path in old_indexes)
    new_indexes = set((project / "work/temporary/indexes").glob("*.idx"))
    assert len(new_indexes) == 1
    assert old_indexes.isdisjoint(new_indexes)
    assert "cleanup deferred" in second.stderr
    assert not list(Path(manifest(project)["output"]).glob("*.idx"))
    # Both subprocesses exited: their disposable files can now be removed by
    # this parent process, confirming no lock is retained across process exit.
    for path in new_indexes:
        cleanup_index(path)
    assert not list((project / "work/temporary/indexes").glob("*.idx"))


def test_cleanup_does_not_suppress_real_permission_errors(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("Access denied")
    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="Access denied"):
        cleanup_index(tmp_path / "example.idx")
