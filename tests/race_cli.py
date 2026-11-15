"""Запуск `strateg` для тестов параллельной записи: сеть-заглушка и пауза «после чтения».

Только для тестов: `python tests/race_cli.py <аргументы strateg>` ведёт себя как `strateg`, но

- `RACE_PAGES` — файл JSON «адрес -> HTML»; загрузчик отдаёт эти страницы без сети;
- `RACE_NET_DELAY` — пауза загрузчика, секунд: за неё соседний процесс успевает записать то же самое;
- `RACE_OPEN_DELAY` — пауза сразу после открытия проекта, до блокировки: пока команда «думает», соседи
  успевают записать своё, и прочитанное при открытии устаревает. Состояние для записи читается заново
  под блокировкой;
- `RACE_WRITE_DELAY` — пауза перед записью реестров `sources.yaml` и `claims.yaml`, секунд. Прочитать,
  подождать, записать: если запись не под блокировкой папки, соседний процесс успевает прочитать то же
  состояние, и тест падает, а не проходит случайно.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from strateg import commands, fetch
from strateg import project as proj
from strateg.cli import main

NET_DELAY = float(os.environ.get("RACE_NET_DELAY", "0"))
WRITE_DELAY = float(os.environ.get("RACE_WRITE_DELAY", "0"))
OPEN_DELAY = float(os.environ.get("RACE_OPEN_DELAY", "0"))


class FakeFetcher:
    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file

    def get(self, url: str) -> fetch.Page:
        time.sleep(NET_DELAY)
        pages = json.loads(Path(os.environ["RACE_PAGES"]).read_text(encoding="utf-8"))
        return fetch.Page(
            url=url,
            status=200,
            content_type="text/html; charset=utf-8",
            body=pages[url].encode("utf-8"),
        )


def slow(method):
    def wrapper(self):
        time.sleep(WRITE_DELAY)
        return method(self)

    return wrapper


if __name__ == "__main__":
    if os.environ.get("RACE_PAGES"):
        commands.make_fetcher = FakeFetcher
    if WRITE_DELAY:
        proj.Project.save_sources = slow(proj.Project.save_sources)
        proj.Project.save_claims = slow(proj.Project.save_claims)
    if OPEN_DELAY:
        real_load = proj.Project.load.__func__

        def slow_load(cls, root, timeout=None):
            project = real_load(cls, root, timeout)
            time.sleep(OPEN_DELAY)
            return project

        proj.Project.load = classmethod(slow_load)
    sys.exit(main(sys.argv[1:]))
