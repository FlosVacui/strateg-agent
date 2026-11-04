"""Проверка относительных ссылок в Markdown-документах репозитория.

Ищет ссылки вида [текст](путь) и [текст](путь#якорь) во всех *.md, кроме
служебных папок, и проверяет, что файл существует, а якорь есть среди
заголовков целевого файла. Внешние ссылки (http, https, mailto) не проверяет.
Понимает `%20` и другие `%XX` в путях и якорях, а у повторяющихся заголовков —
GitHub-суффиксы `-1`, `-2`. Личные проекты `projects/*` пропускает, кроме
`projects/example` и `projects/demo`.

Запуск: python scripts/check_links.py [корень]   (по умолчанию — корень репозитория)
Код возврата: 0 — всё в порядке, 1 — есть битые ссылки.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
}
TRACKED_PROJECTS = {"example", "demo"}
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
CODE_FENCE_RE = re.compile(r"^```.*?^```", re.S | re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def slug(heading: str) -> str:
    """Якорь заголовка в стиле GitHub: нижний регистр, пробелы в дефисы, без знаков."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Якоря заголовков файла; повтор получает суффикс -1, -2, как на GitHub."""
    text = CODE_FENCE_RE.sub("", path.read_text(encoding="utf-8-sig"))
    seen: dict[str, int] = {}
    result: set[str] = set()
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        base = slug(m.group(1))
        n = seen.get(base, 0)
        result.add(base if n == 0 else f"{base}-{n}")
        seen[base] = n + 1
    return result


def is_skipped(rel: Path) -> bool:
    """Служебные папки и личные проекты пользователя не проверяем."""
    parts = rel.parts
    if SKIP_DIRS.intersection(parts):
        return True
    return len(parts) > 2 and parts[0] == "projects" and parts[1] not in TRACKED_PROJECTS


def find_problems(root: Path) -> tuple[int, list[str]]:
    """Возвращает число проверенных файлов и список сообщений о битых ссылках."""
    root = root.resolve()
    problems: list[str] = []
    files = [p for p in root.rglob("*.md") if not is_skipped(p.relative_to(root))]
    for md in sorted(files):
        text = CODE_FENCE_RE.sub("", md.read_text(encoding="utf-8-sig"))
        text = INLINE_CODE_RE.sub("", text)
        for m in LINK_RE.finditer(text):
            target = m.group(1)
            if re.match(r"^[a-z]+:", target) or target.startswith("#") and not target[1:]:
                continue
            file_part, _, anchor = target.partition("#")
            file_part, anchor = unquote(file_part), unquote(anchor)
            dest = (md.parent / file_part).resolve() if file_part else md
            line = text.count("\n", 0, m.start()) + 1
            where = f"{md.relative_to(root).as_posix()}:{line}"
            if not dest.exists():
                problems.append(f"{where}: нет файла {target}")
            elif anchor and dest.suffix == ".md" and anchor.lower() not in anchors(dest):
                problems.append(f"{where}: нет якоря #{anchor} в {file_part or md.name}")
    return len(files), problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent
    checked, problems = find_problems(root)
    for p in problems:
        print(p)
    print(f"Проверено файлов: {checked}, битых ссылок: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
