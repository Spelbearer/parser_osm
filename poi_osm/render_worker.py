"""Internal export process: exit closes native handles before directory publication."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .product import read, render
from .runtime import configure_utf8


def main():
    configure_utf8()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    job = read(Path(sys.argv[1]))
    for field in ('registry', 'root', 'work', 'review', 'reuse'):
        if job[field] is not None:
            job[field] = Path(job[field])
    bundle, report = render(**job)
    print(json.dumps({'bundle':str(bundle), 'report':report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
