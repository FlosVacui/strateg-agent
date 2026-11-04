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


def _check(name: str, ok: bool, detail: str) -> bool:
    mark = "OK " if ok else "НЕТ"
    print(f"[{mark}] {name}: {detail}")
    return ok


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Проверка среды: Python, uv, git, кодировка, запись в папку проекта."""
    results = []
    py_ok = sys.version_info >= (3, 12)
    results.append(_check("Python", py_ok, platform.python_version()))
    for tool in ("uv", "git"):
        path = shutil.which(tool)
        results.append(_check(tool, path is not None, path or "не найден в PATH"))
    enc = sys.stdout.encoding or locale.getpreferredencoding(False)
    results.append(_check("Кодировка вывода", enc.lower().replace("-", "") == "utf8", enc))
    root = Path.cwd()
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".strateg-doctor-", delete=True):
            pass
        writable = True
    except OSError as exc:  # нет прав на запись
        writable = False
        root = Path(f"{root} ({exc})")
    results.append(_check("Запись в папку", writable, str(root)))
    return 0 if all(results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strateg", description="CLI агента «Стратег»")
    parser.add_argument("--version", action="version", version=f"strateg {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="проверить среду").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
