"""Запуск Playwright MCP для `.mcp.json` (ADR-0013): одинаково на Windows и macOS, без обёртки `cmd /c`.

Среда запускает `uv run --frozen python -X utf8 scripts/playwright_mcp.py`; скрипт находит `npx`
(`shutil.which`: на Windows это `npx.cmd`) и отдаёт ему стандартный ввод и вывод как есть: JSON-RPC идёт
по ним без буферизации. Версия пакета зафиксирована. Только стандартная библиотека.

Браузер: Windows — `msedge`, macOS и Linux — `chrome`; `STRATEG_BROWSER` переопределяет.
Снимки — в `<временная папка ОС>/strateg-shots`, вне папки проекта; их забирает `strateg ux import`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

PACKAGE = "@playwright/mcp@0.0.84"
SHOTS_NAME = "strateg-shots"  # = strateg.ux_targets.SHOTS_NAME


def browser_for(platform: str, env: Mapping[str, str]) -> str:
    chosen = env.get("STRATEG_BROWSER", "").strip()
    if chosen:
        return chosen
    return "msedge" if platform == "win32" else "chrome"


def build_command(npx: str, platform: str, env: Mapping[str, str], temp: str) -> list[str]:
    return [
        npx,
        "-y",
        PACKAGE,
        "--headless",
        "--isolated",
        "--snapshot-mode",
        "none",  # текст страницы в ответы на действия не идёт
        "--file-paths",
        "absolute",
        "--browser",
        browser_for(platform, env),
        "--output-dir",
        str(Path(temp) / SHOTS_NAME),
    ]


def main() -> int:
    npx = shutil.which("npx")
    if npx is None:
        print("npx не найден: для снимков экрана нужен Node 22 или новее", file=sys.stderr)
        return 1
    argv = build_command(npx, sys.platform, os.environ, tempfile.gettempdir())
    if sys.platform == "win32":  # `execv` на Windows не заменяет процесс: стандартные потоки наследуются
        return subprocess.call(argv)
    os.execv(npx, argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
