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
original = Path.unlink
def unlink(self, *args, **kwargs):
    if self.suffix == '.idx':
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
    assert list((project / "work/temporary/indexes").glob("*.idx"))
    monkeypatch.setenv("PYTHONPATH", str(repo))
    invoke(project)
    assert manifest(project)["metrics"]["changes"] == {"UNCHANGED": 4}
    assert not list((project / "work/temporary/indexes").glob("*.idx"))


def test_cleanup_does_not_suppress_real_permission_errors(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("Access denied")
    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="Access denied"):
        cleanup_index(tmp_path / "example.idx")
