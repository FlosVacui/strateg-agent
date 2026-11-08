"""Служебный файл учёта стоимости `.tmp/cost/offsets.json`: смещения разбора транскриптов и время пересчёта.

Файл вне файлов выхода и в архив проекта не попадает. Модуль лёгкий (без `yaml` и `pydantic`): его читает
хук `PreToolUse` на каждом вызове, до того как решит, нужны ли тяжёлые модули. Пишет файл `strateg.accounting`
под блокировкой папки проекта.

Что в файле:
- `files` — по ключу транскрипта: смещение, отпечаток начала файла, число пропущенных записей;
- `recounted_at` — время прошлого пересчёта (секунды Unix): по нему хук не пересчитывает расход чаще
  раза в несколько секунд;
- `exhausted` — был ли бюджет исчерпан на момент прошлого пересчёта (`true`, `false` или нет, если проект
  не удалось прочитать): пока пересчёт свежий и бюджет не был исчерпан, хуку незачем читать проект заново.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SERVICE_DIR = ".tmp/cost"
OFFSETS_FILE = "offsets.json"
OFFSETS_VERSION = 1
RECOUNTED_KEY = "recounted_at"
EXHAUSTED_KEY = "exhausted"


def offsets_path(root: Path) -> Path:
    return root / SERVICE_DIR / OFFSETS_FILE


def load_offsets(root: Path) -> dict[str, Any]:
    """Файл смещений; нет файла или он не читается — пустые смещения (разбор начнётся с начала)."""
    try:
        data = json.loads(offsets_path(root).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return {"version": OFFSETS_VERSION, "files": {}}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or data.get("version") != OFFSETS_VERSION:
        return {"version": OFFSETS_VERSION, "files": {}}
    out: dict[str, Any] = {"version": OFFSETS_VERSION, "files": files}
    when = data.get(RECOUNTED_KEY)
    if isinstance(when, int | float) and not isinstance(when, bool):
        out[RECOUNTED_KEY] = float(when)
    if isinstance(data.get(EXHAUSTED_KEY), bool):
        out[EXHAUSTED_KEY] = data[EXHAUSTED_KEY]
    return out


def wall() -> float:
    """Текущее время, секунды Unix. Отдельная функция: тесты подставляют часы."""
    return time.time()


def recount_state(root: Path) -> tuple[float | None, bool | None]:
    """Сколько секунд назад был пересчёт и был ли тогда исчерпан бюджет; None — нет сведений.

    Пересчёт «из будущего» (часы переведены назад) считается неизвестным.
    """
    data = load_offsets(root)
    when = data.get(RECOUNTED_KEY)
    if when is None:
        return None, None
    ago = wall() - when
    if ago < 0:
        return None, None
    return ago, data.get(EXHAUSTED_KEY)
