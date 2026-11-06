"""Генерация JSON Schema файлов проекта из моделей `src/strateg/models.py` в папку `schemas/`.

Вывод детерминированный: UTF-8, окончания LF, `ensure_ascii=False`, порядок ключей задают модели.

Запуск: python scripts/gen_schemas.py            — записать схемы
        python scripts/gen_schemas.py --check    — сверить файлы со сгенерированным (для CI)
Код возврата: 0 — всё в порядке, 1 — схемы расходятся с моделями.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from strateg.models import SCHEMA_MODELS

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
SUFFIX = ".schema.json"


def render(name: str) -> str:
    """Текст схемы одного файла: заголовок диалекта и схема модели."""
    schema = {"$schema": DIALECT, **SCHEMA_MODELS[name].model_json_schema()}
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def expected() -> dict[str, str]:
    return {name + SUFFIX: render(name) for name in SCHEMA_MODELS}


def find_problems(folder: Path = SCHEMAS_DIR) -> list[str]:
    """Расхождения папки со сгенерированными схемами."""
    problems = []
    want = expected()
    for fname, text in want.items():
        path = folder / fname
        if not path.is_file():
            problems.append(f"нет файла схемы {fname}")
        elif path.read_bytes() != text.encode("utf-8"):
            problems.append(f"схема {fname} не совпадает с моделью")
    if folder.is_dir():
        problems += [f"лишний файл {p.name}" for p in sorted(folder.glob("*" + SUFFIX)) if p.name not in want]
    return problems


def write(folder: Path = SCHEMAS_DIR) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for fname, text in expected().items():
        (folder / fname).write_bytes(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Генерация JSON Schema из моделей проекта.")
    parser.add_argument("--check", action="store_true", help="только сверить, ничего не записывая")
    args = parser.parse_args(argv)
    if args.check:
        problems = find_problems()
        for p in problems:
            print(p)
        if problems:
            print("Запустите `uv run python scripts/gen_schemas.py` и добавьте изменённые схемы в коммит.")
            return 1
        print(f"Схем: {len(SCHEMA_MODELS)}, расхождений: 0")
        return 0
    write()
    print(f"Записано схем: {len(SCHEMA_MODELS)} в {SCHEMAS_DIR.name}/")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
