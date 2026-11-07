"""Все проверки CI одной командой, локально: те же шаги, что в `.github/workflows/ci.yml`.

Запуск из корня репозитория:  uv run --no-project python scripts/check_all.py
  --no-tests   без pytest (быстрая проверка перед коммитом)

Скрипт берёт только стандартную библиотеку и запускается без окружения проекта: первым шагом
`uv sync --locked` пересобирает `.venv`, а запущенный из него Python на Windows этому мешал бы.
Каждый шаг идёт до конца, даже если предыдущий упал; итог — таблица и код 1 при любой ошибке.
Проверяется одна ОС — та, на которой запущено; macOS и Linux проверяет только CI.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Шаги задачи `test` в ci.yml — те же команды, в том же порядке (это сверяет тест).
STEPS: list[tuple[str, list[str]]] = [
    ("Зависимости по uv.lock", ["uv", "sync", "--locked"]),
    ("Проверка среды", ["uv", "run", "strateg", "doctor"]),
    ("Ruff — ошибки", ["uv", "run", "ruff", "check", "."]),
    ("Ruff — формат", ["uv", "run", "ruff", "format", "--check", "."]),
    ("Тесты", ["uv", "run", "pytest", "-q"]),
    ("Ссылки в документах", ["uv", "run", "python", "scripts/check_links.py"]),
    ("Схемы совпадают с моделями", ["uv", "run", "python", "scripts/gen_schemas.py", "--check"]),
    ("Копия скиллов совпадает с источником", ["uv", "run", "python", "scripts/sync_skills.py", "--check"]),
]
TESTS_STEP = "Тесты"
SERVICE_DOMAIN = "@users.noreply.github.com"
SERVICE_ADDRESS = "noreply@github.com"


def run(argv: list[str]) -> tuple[int, str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {argv[0]}"
    return r.returncode, (r.stdout + r.stderr).strip()


def gitleaks() -> tuple[int | None, str]:
    """Секреты во всей истории, как задача `secrets` в CI. Нет gitleaks — шаг пропущен (None)."""
    if shutil.which("gitleaks") is None:
        return None, "gitleaks нет в PATH — пропущено; в CI он есть"
    return run(["gitleaks", "git", "--no-banner", "--redact", "."])


def bad_author_addresses(log: str) -> list[str]:
    """Коммиты, где у автора или коммитера не служебный адрес GitHub. Строки лога — «хеш автор коммитер»."""
    bad = []
    for line in log.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if any(not (a.endswith(SERVICE_DOMAIN) or a == SERVICE_ADDRESS) for a in parts[1:3]):
            bad.append(parts[0])
    return bad


def author_addresses() -> tuple[int, str]:
    code, out = run(["git", "log", "--no-merges", "--format=%h %ae %ce"])
    if code != 0:
        return code, out
    bad = bad_author_addresses(out)
    if bad:
        return 1, "коммиты с личным адресом автора или коммитера: " + " ".join(bad)
    return 0, "все адреса в истории служебные"


def tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.splitlines()[-lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Все проверки CI локально")
    parser.add_argument("--no-tests", action="store_true", help="без pytest")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    results: list[tuple[str, str, float]] = []
    checks = [(name, lambda a=a: run(a)) for name, a in STEPS if not (args.no_tests and name == TESTS_STEP)]
    checks += [("Секреты (gitleaks)", gitleaks), ("Адреса авторов — только служебные", author_addresses)]
    for name, check in checks:
        print(f"→ {name}", flush=True)
        start = time.monotonic()
        code, out = check()
        spent = time.monotonic() - start
        if code is None:
            status = "пропущено"
        elif code == 0:
            status = "ок"
        else:
            status = "ошибка"
            print(tail(out), flush=True)
        results.append((name, status, spent))

    print("\nИтог:")
    for name, status, spent in results:
        print(f"  {status:<9} {name} ({spent:.0f} с)")
    if args.no_tests:
        print("  пропущено Тесты (--no-tests)")
    print("Проверена только эта ОС; macOS и Linux проверяет CI.")
    failed = [name for name, status, _ in results if status == "ошибка"]
    if failed:
        print("Не прошло: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
