"""Тест-страж: в продукте нет служебного штаба, графа знаний, личных путей и имени автора.

Строки поиска собраны из частей, чтобы тест не находил сам себя.
Проверяются файлы, которые попадут в репозиторий: отслеживаемые git и новые, не закрытые
`.gitignore`. Без git (например, из архива) — обход папки без `.venv` и `.git`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = {
    "штаб команды": "strateg" + "-" + "hq",
    "граф знаний": "graph" + "ify",
    "личный путь (\\)": "C:" + "\\" + "Users",
    "личный путь (/)": "C:" + "/" + "Users",
    "имя автора": "Flos" + "Vacui",
}
# Исключения: путь файла от корня -> какие ключи FORBIDDEN в нём допустимы.
# В LICENSE и NOTICE имя автора уместно (развилка Р-1).
EXCEPTIONS: dict[str, set[str]] = {"LICENSE": {"имя автора"}, "NOTICE": {"имя автора"}}

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


def _walk() -> list[Path]:
    return [
        p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    ]


def _git_toplevel() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out).resolve() if out else None


def candidate_files() -> list[Path]:
    # Архив, распакованный внутри чужого репозитория: git покажет файлы родителя — обходим папку.
    if _git_toplevel() != ROOT.resolve():
        return _walk()
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return _walk()
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def test_forbidden_strings_assembled_from_parts_are_not_in_this_file():
    own = Path(__file__).read_text(encoding="utf-8").lower()
    for needle in FORBIDDEN.values():
        assert needle.lower() not in own


def test_there_are_files_to_check():
    assert len(candidate_files()) > 10


def test_no_internal_traces_in_product():
    found = []
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_bytes().decode("utf-8", "ignore").lower()
        for label, needle in FORBIDDEN.items():
            if label in EXCEPTIONS.get(rel, set()):
                continue
            if needle.lower() in text or needle.lower() in rel.lower():
                found.append(f"{rel}: {label}")
    assert not found, "служебное в продукте:\n" + "\n".join(found)
