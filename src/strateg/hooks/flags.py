"""Переменные среды хуков (ADR-0009). Их задаёт человек при запуске среды.

- `STRATEG_HOOKS=0` выключает все хуки: только для замера их пользы.
- `STRATEG_DEV=1` выключает только охрану правил (`AGENTS.md`, `CLAUDE.md`, `.claude/`): для разработки
  самого агента. Права ролей, `.env`, `~/.strateg/` и `.tmp/pending/` остаются в силе.
"""

from __future__ import annotations

from collections.abc import Mapping

HOOKS_ENV = "STRATEG_HOOKS"
DEV_ENV = "STRATEG_DEV"


def hooks_off(env: Mapping[str, str]) -> bool:
    return env.get(HOOKS_ENV, "").strip() == "0"


def dev_mode(env: Mapping[str, str]) -> bool:
    return env.get(DEV_ENV, "").strip() == "1"


def notes(env: Mapping[str, str]) -> list[str]:
    """Что из переменных задано: строки для `doctor` и `status --brief` (пусто, если ничего)."""
    out = []
    if hooks_off(env):
        out.append(f"{HOOKS_ENV}=0: все хуки выключены, права ролей и сводка при старте не действуют")
    if dev_mode(env):
        out.append(f"{DEV_ENV}=1: охрана правил (AGENTS.md, CLAUDE.md, .claude/) выключена")
    return out
