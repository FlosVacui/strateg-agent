"""Копия скиллов для других сред: .claude/skills → .agents/skills.

Основная копия скиллов — .claude/skills (Claude Code). Codex, Cursor, OpenCode и Copilot
ищут скиллы в .agents/skills. Симлинки git на Windows ломает, поэтому храним копию.

Запуск: python scripts/sync_skills.py          — обновить копию
        python scripts/sync_skills.py --check  — проверить совпадение (для CI), код 1 при расхождении
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / ".claude" / "skills"
DST = ROOT / ".agents" / "skills"


def files(base: Path) -> set[Path]:
    if not base.exists():
        return set()
    return {p.relative_to(base) for p in base.rglob("*") if p.is_file()}


def check() -> int:
    a, b = files(SRC), files(DST)
    diff = sorted(a ^ b) + sorted(p for p in a & b if not filecmp.cmp(SRC / p, DST / p, shallow=False))
    for p in diff:
        print(f"расходится: {p.as_posix()}")
    print(f"Скиллов-файлов: {len(a)}, расхождений: {len(diff)}")
    return 1 if diff else 0


def sync() -> int:
    if DST.exists():
        shutil.rmtree(DST)
    if SRC.exists():
        shutil.copytree(SRC, DST)
    print(f"Скопировано файлов: {len(files(DST))}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(check() if "--check" in sys.argv[1:] else sync())
