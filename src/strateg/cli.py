"""Точка входа CLI `strateg`.

Каркас этапа 0: команды `--version` и `doctor`. Остальные команды появятся в пакете 3.2.
"""

from __future__ import annotations

import argparse
import locale
import platform
import shutil
import sys
import tempfile
from pathlib import Path

from strateg import __version__

OK, WARN, ERR = "OK", "WARN", "ERR"
_MARKS = {OK: "OK  ", WARN: "ПРЕД", ERR: "НЕТ "}

CHCP_HINT = "выполните `chcp 65001` или откройте Windows Terminal"


def _is_utf8(name: str) -> bool:
    return name.lower().replace("-", "").replace("_", "") == "utf8"


def _windows_console_cp() -> int:
    """Кодовая страница вывода консоли Windows; 0 — консоли нет (например, в CI)."""
    import ctypes

    return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]


def _stdout_encoding() -> str:
    return sys.stdout.encoding or locale.getpreferredencoding(False)


def detect_console_encoding() -> tuple[str, str]:
    """Уровень и пояснение для кодировки консоли.

    Вызывать ДО `sys.stdout.reconfigure`: после перенастройки Python всегда говорит utf-8,
    даже если консоль показывает текст в cp866 или cp1251.
    """
    if sys.platform == "win32":
        try:
            cp = _windows_console_cp()
        except (AttributeError, OSError):
            return WARN, f"кодовую страницу консоли определить не удалось; если текст искажён, {CHCP_HINT}"
        if cp == 65001:
            return OK, "UTF-8 (кодовая страница 65001)"
        if cp == 0:
            return OK, "консоли нет, вывод идёт в файл или канал"
        return WARN, f"кодовая страница консоли {cp}, кириллица может исказиться: {CHCP_HINT}"
    enc = _stdout_encoding()
    if _is_utf8(enc):
        return OK, enc
    return WARN, f"{enc}, кириллица может исказиться: задайте UTF-8 (например, LANG=ru_RU.UTF-8)"


def find_repo_root() -> Path | None:
    """Корень репозитория — ближайшая папка с pyproject.toml (от модуля, затем от текущей папки)."""
    for start in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (start, *start.parents):
            if (parent / "pyproject.toml").is_file():
                return parent
    return None


def _check_writable(folder: Path) -> tuple[str, str]:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".strateg-doctor-", delete=True):
            pass
    except OSError as exc:
        return ERR, f"{folder} ({exc})"
    return OK, str(folder)


def _report(name: str, level: str, detail: str) -> str:
    print(f"[{_MARKS[level]}] {name}: {detail}")
    return level


def cmd_doctor(args: argparse.Namespace) -> int:
    """Проверка среды: Python, uv, git, кодировка, запись в `projects/`.

    Ошибки дают код 1. Предупреждения (нет git, не UTF-8 в консоли) код не меняют.
    """
    levels = []
    levels.append(_report("Python", OK if sys.version_info >= (3, 12) else ERR, platform.python_version()))
    uv = shutil.which("uv")
    levels.append(_report("uv", OK if uv else ERR, uv or "не найден в PATH"))
    git = shutil.which("git")
    levels.append(
        _report("git", OK, git)
        if git
        else _report(
            "git", WARN, "не найден; для работы с проектом не обязателен, но нужен для истории изменений"
        )
    )
    console = getattr(args, "console", None) or detect_console_encoding()
    levels.append(_report("Кодировка консоли", *console))
    root = find_repo_root()
    if root is None:
        levels.append(_report("Запись в projects/", ERR, "не найден корень репозитория (pyproject.toml)"))
    elif not (root / "projects").is_dir():
        levels.append(_report("Запись в projects/", ERR, f"нет папки {root / 'projects'}"))
    else:
        levels.append(_report("Запись в projects/", *_check_writable(root / "projects")))
    return 1 if ERR in levels else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strateg", description="CLI агента «Стратег»")
    parser.add_argument("--version", action="version", version=f"strateg {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="проверить среду").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    console = detect_console_encoding()  # до reconfigure, иначе проверка всегда «OK»
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    args.console = console
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
