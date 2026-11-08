"""Синтетические транскрипты для тестов учёта стоимости: только поля учёта, без текста сообщений.

Настоящие транскрипты в репозиторий не копируются: в них текст разговора. Здесь строка ассистента — это
`type`, `message.id`, `message.model` и `message.usage` в том виде, в каком их пишет Claude Code.
"""

from __future__ import annotations

import json
from pathlib import Path

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"


def assistant(
    mid: str,
    model: str = SONNET,
    *,
    inp: int = 0,
    out: int = 0,
    cache_read: int = 0,
    write_5m: int = 0,
    write_1h: int = 0,
    web: int | None = None,
    geo: str | None = None,
    speed: str | None = None,
) -> str:
    usage: dict = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": write_5m + write_1h,
        "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": write_1h},
    }
    if web is not None:
        usage["server_tool_use"] = {"web_search_requests": web, "web_fetch_requests": 0}
    if geo is not None:
        usage["inference_geo"] = geo
    if speed is not None:
        usage["speed"] = speed
    return json.dumps(
        {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}}, ensure_ascii=False
    )


def user_line() -> str:
    return json.dumps({"type": "user", "message": {"role": "user"}})


def write(path: Path, *lines: str, newline_at_end: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + ("\n" if newline_at_end and lines else "")
    path.write_bytes(text.encode("utf-8"))
    return path


def append(path: Path, *lines: str) -> None:
    with path.open("ab") as f:
        f.write(("\n".join(lines) + "\n").encode("utf-8"))


def session_files(base: Path, session: str = "sess-1") -> tuple[Path, Path]:
    """Основной транскрипт `<base>/<сессия>.jsonl` и папка субагентов `<base>/<сессия>/subagents/`."""
    return base / f"{session}.jsonl", base / session / "subagents"


def agent_file(folder: Path, agent_id: str, *lines: str, agent_type: str | None = None) -> Path:
    path = write(folder / f"agent-{agent_id}.jsonl", *lines)
    if agent_type is not None:
        (folder / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": agent_type}), encoding="utf-8"
        )
    return path
