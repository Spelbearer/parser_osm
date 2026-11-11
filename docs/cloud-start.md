# Cloud development

Use `/workspace/parser_osm` and the existing `.venv/bin/python`. The cloud dependency bootstrap remains `scripts/cloud_install.sh`; it installs the pinned development requirements and runs tests. Do not change or publish the cloud environment as part of normal repository work.

Run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python run.py --version
```

Tests create real small synthetic PBFs in pytest temporary directories. No downloaded Russia dataset, credentials or external service is required for tests. There is no permanent web service. Python 3.11 and 3.13 are supported; GitHub Actions checks Linux and Windows.

The only production entry is `run.py`; see README.md. New installations require reviewed WGS84 boundaries and the complete local federal-district extract set. Existing installations keep their state and can use `--adopt` once to migrate the old output layout. Do not bypass source hashes, dates, geographic coverage, quality checks or the publication lock. Production data and user-uploaded attachments are not test fixtures.
