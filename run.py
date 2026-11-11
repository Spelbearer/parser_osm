"""Основной скрипт POI: python run.py --root D:\\osm_results [--adopt | --files]."""
import logging

from poi_osm.product import main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        logging.error('%s',error)
        raise SystemExit(1)
