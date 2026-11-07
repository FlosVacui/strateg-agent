"""Копия скиллов по запросу: .claude/skills → папка из флага.

Скиллы живут только в .claude/skills; копии в репозитории нет. Codex ищет скиллы в .agents/skills
и не читает .claude/skills. Симлинки git на Windows ломает, поэтому копию делает скрипт, когда она нужна.

Запуск: python scripts/sync_skills.py --dest .agents/skills  — создать копию в папке
        python scripts/sync_skills.py --check                — проверить, что скрипт даёт копию,
                                                               совпадающую с источником (для CI)
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / ".claude" / "skills"


def files(base: Path) -> set[Path]:
    if not base.exists():
        return set()
    return {p.relative_to(base) for p in base.rglob("*") if p.is_file()}


def skill_dirs(base: Path) -> list[Path]:
    """Папки скиллов: в каждой есть SKILL.md."""
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def copy_skills(dest: Path) -> int:
    """Копирует скиллы из SRC в dest. Возвращает число скопированных файлов.

    Заменяет только папки скиллов с теми же именами, чужие файлы в dest не трогает.
    """
    dest = dest.resolve()
    src = SRC.resolve()
    if dest == src or src in dest.parents:
        raise ValueError(f"копия в {dest} затронула бы источник: выберите другую папку")
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill in skill_dirs(src):
        target = dest / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        count += len(files(target))
    return count


def check() -> int:
    """Копия во временную папку совпадает с источником: те же файлы и то же содержимое."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp)
        copy_skills(copy)
        # файлы вне папок скиллов (например, .gitkeep в корне) в копию не идут
        source = {p for p in files(SRC) if len(p.parts) > 1}
        made = files(copy)
        diff = sorted(source ^ made)
        diff += sorted(p for p in source & made if not filecmp.cmp(SRC / p, copy / p, shallow=False))
    for p in diff:
        print(f"расходится: {p.as_posix()}")
    print(f"Файлов скиллов: {len(source)}, расхождений: {len(diff)}")
    return 1 if diff or not source else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Копия скиллов .claude/skills по запросу")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dest",
        type=Path,
        metavar="ПАПКА",
        help="создать копию скиллов в этой папке, например .agents/skills",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="проверить, что скрипт даёт копию, совпадающую с источником, во временной папке "
        "(ничего не создаёт; код 1 при расхождении)",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check()
    try:
        count = copy_skills(args.dest)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Скопировано файлов: {count}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
