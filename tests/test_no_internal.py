"""Тест-страж: в продукте нет служебного штаба, графа знаний, личных путей и имени автора,
а также номеров замечаний ревью и заданий и ссылок на рабочую папку заданий.

Строки поиска собраны из частей, чтобы тест не находил сам себя.
Проверяются файлы, которые попадут в репозиторий: отслеживаемые git и новые, не закрытые
`.gitignore`. Без git (например, из архива) — обход папки без `.venv` и `.git`.
"""

from __future__ import annotations

import re
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
# Номера замечаний и заданий: шаблоны, тоже собранные из частей.
PATTERNS = {
    "номер замечания ревью": re.compile(r"\b" + "R" + "V-"),
    "номер замечания вида R-n.n-n": re.compile(r"\bR-\d+\.\d+-\d+"),
    "папка заданий": re.compile(r"\b" + "brief" + r"s\b", re.IGNORECASE),
}
# Исключения: путь файла от корня -> какие ключи FORBIDDEN в нём допустимы.
# В LICENSE и NOTICE имя автора уместно (развилка Р-1).
EXCEPTIONS: dict[str, set[str]] = {
    "LICENSE": {"имя автора"},
    "NOTICE": {"имя автора"},
    "pyproject.toml": {"имя автора"},  # адрес репозитория в [project.urls] и в User-Agent
}

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
    text = Path(__file__).read_text(encoding="utf-8")
    for needle in FORBIDDEN.values():
        assert needle.lower() not in text.lower()
    for label, pattern in PATTERNS.items():
        assert not pattern.search(text), label


def test_patterns_catch_review_and_brief_numbers():
    samples = {
        "номер замечания ревью": "см. " + "RV" + "-2.3-1",
        "номер замечания вида R-n.n-n": "замечание " + "R" + "-2.3-14",
        "папка заданий": "docs/" + "brief" + "s/2.3.md",
    }
    for label, sample in samples.items():
        assert PATTERNS[label].search(sample), label
    for harmless in ("SERV-1", "OBSERV-2", "R-значение", "версия 2.3-1", "нет брифов"):
        assert not any(p.search(harmless) for p in PATTERNS.values()), harmless


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
        original = path.read_bytes().decode("utf-8", "ignore")
        for label, pattern in PATTERNS.items():
            if pattern.search(original) or pattern.search(rel):
                found.append(f"{rel}: {label}")
    assert not found, "служебное в продукте:\n" + "\n".join(found)
