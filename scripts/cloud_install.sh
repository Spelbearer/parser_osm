#!/usr/bin/env bash
set -eu
cd /workspace/parser_osm
export UV_CACHE_DIR=/workspace/.cache/uv
export UV_PYTHON_INSTALL_DIR=/workspace/.python
uv python install --no-bin 3.11
if [ ! -x .venv/bin/python ]; then
    uv venv --python 3.11 .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 11), "Use a Python 3.11 virtual environment"'
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
