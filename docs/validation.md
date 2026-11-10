# Проверка реализации

Дата: 2026-11-10. Среда: Linux x86_64, CPython 3.11.17. Версии зависимостей закреплены в requirements*.txt.

Выполнено:

```text
bash scripts/cloud_install.sh
49 passed in 13.73s

.venv/bin/python scripts/make_demo.py
.venv/bin/python main.py --config demo/settings.json --mode full
# 4 NEW: две node-категории, площадной way, multipolygon relation

.venv/bin/python main.py --config demo/settings.json --mode incremental --duplicates
# 4 UNCHANGED; NEW / UPDATED / DELETED отсутствуют

.venv/bin/python main.py --config demo/settings.json --check
# errors: [], exit 0
```

Тесты используют настоящие небольшие PBF с искусственными геометриями. Проверены смена снимков и категорий, все четыре статуса, сохранение first_seen при возвращении записи, типовые и пространственные дубли, раздельные региональные состояния, парсинг node/way/relation, внутренняя точка вогнутого полигона и антимеридиан. Также проверены контрольные суммы, давность, повторные попытки загрузки через HTTP-дубли, сохранение предыдущей версии при сбоях и аномалиях, блокировка второго процесса, read-only режим --check, CSV с BOM/экранированием/переносами и Parquet с DATE/null и фильтрацией региона.

Производственный `main.py --check` ожидаемо вернул exit 1: нет проверенного `boundaries/regions.geojson`, `boundaries_reviewed=false`, источник Geofabrik недоступен через сетевой прокси (403 на CONNECT). Проверка актуальной даты/размера/условий источника и реального региона не выполнена. Измеренные 0.1 секунды демонстрационного запуска неприменимы к оценке России.

Не проверялись: полноценная выгрузка России, реальные региональные границы, производительность на полном PBF, Windows и работа Планировщика, исполнение SQL в Teradata. Тесты HTTP используют воспроизводимые ответы, а не подтверждают доступность реального издателя.

Сохранение полей install_script/start_skill облачной конфигурации завершилось ошибкой INVALID_ARGUMENT; платформа не подтвердила сохранение. Инструкции доступны локально в scripts/cloud_install.sh и docs/cloud-start.md. Публикация облачной среды не выполнялась.
