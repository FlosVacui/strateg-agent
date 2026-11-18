"""Учёт стоимости по транскриптам среды (ADR-0010): новые сообщения ассистента — записи `cost.jsonl`.

Вызывают хуки `Stop`, `SubagentStop` и `PreToolUse` перед платным вызовом (`strateg.hooks`). Что делает
`account`:

1. под блокировкой папки проекта (`strateg.storage`) читает смещения разбора из служебного файла
   `.tmp/cost/offsets.json` — вне файлов выхода, в архив проекта он не попадает;
2. читает каждый транскрипт с места, где остановился разбор (`strateg.transcript`);
3. сообщение, id которого уже есть в `cost.jsonl`, пропускает: повторный разбор и копия истории в другой
   сессии (ответвление, продолжение) расход не удваивают;
4. для новых сообщений считает цену по `config/prices.yaml` (`strateg.pricing`); модели нет в таблице —
   токены записываются, цена «нет данных». Вызовы `WebSearch` в сообщении входят в цену (плата за поиск и
   запрос к модели поиска), их доля — в `web_search_usd` записи;
   в копии проекта (`strateg new --from`) сообщения, записанные в транскрипте раньше события «копия проекта»,
   — расход источника: в учёт копии они не входят (сообщение без времени считается новым);
5. дописывает записи в `cost.jsonl` тем же кодом, что журнал (`storage.append_tracked`), и сохраняет смещения,
   время пересчёта и сведение, исчерпан ли бюджет (`strateg.offsets`): по ним хук решает, пересчитывать ли
   расход перед вызовом, который платным не бывает.

В `cost.jsonl` идут только числа, id сообщения, модель, роль и шаг: текст разговора не читается и не хранится.
Хеш `cost.jsonl` в `state.yaml` обновляется вместе с записью, поэтому «изменено вне агента» не возникает.
Кэш `budget.spent_usd` в `state.yaml` хук не трогает: его пересчитывает следующая команда CLI, а расход
для бюджета (`budget.measure`) берётся из `cost.jsonl`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from strateg import budget, offsets, pricing, storage
from strateg import project as proj
from strateg import transcript as tr
from strateg.models import SCHEMA_VERSION, CostRecord, JournalEvent
from strateg.offsets import load_offsets, offsets_path

ENVIRONMENT = "claude-code"
MAIN_ROLE = "основной"
INTERNAL_ROLE = "служебный"  # внутренние агенты среды (подсказки, `/btw`): `agent_type` пуст


@dataclass(frozen=True)
class Source:
    """Транскрипт для разбора: ключ в файле смещений, путь и роль, под которой пишется расход."""

    key: str
    path: Path
    role: str
    required: bool = True  # нет файла: у обязательного — сбой учёта, у необязательного — пропуск


@dataclass
class Result:
    added: int = 0
    duplicates: int = 0
    skipped: int = 0  # пропущено за этот разбор: битые строки и сообщения без учётных полей
    usd: float = 0.0  # цена добавленных сообщений там, где она известна
    unpriced: list[str] = field(default_factory=list)  # модели без цены среди добавленных, по разу
    earlier: int = 0  # сообщения старше копирования проекта: расход источника, в этот проект не идут


# --- Смещения (лёгкая часть — `strateg.offsets`: её читает хук без тяжёлых модулей) -------------------------


def skipped_total(root: Path) -> int:
    """Сколько записей транскриптов пропущено за всё время (для `cost report`)."""
    total = 0
    for entry in load_offsets(root)["files"].values():
        count = entry.get("skipped") if isinstance(entry, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            total += count
    return total


def _exhausted(root: Path, seconds: float) -> bool | None:
    """Исчерпан ли бюджет после этого пересчёта; None — проект не прочитать (хук тогда проверит сам)."""
    try:
        p = proj.Project.load(root, timeout=seconds)
        return bool(budget.exhausted_reasons(budget.measure(p)))
    except Exception:  # noqa: BLE001 - подсказка для хука необязательна: учёт от неё не зависит
        return None


def _save_offsets(root: Path, data: Mapping[str, Any], timeout: float) -> None:
    folder = root / offsets.SERVICE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    storage.atomic_write_bytes(offsets_path(root), text.encode("utf-8"), timeout)


def copy_cut(root: Path) -> dt.datetime | None:
    """Время события «копия проекта»; None — проект не копия (или журнал не прочитать)."""
    try:
        raw = (root / storage.JOURNAL_FILE).read_bytes()
        if proj.COPY_ACTION.encode("utf-8") not in raw:
            return None
        return proj.copy_time(proj.read_jsonl(root / storage.JOURNAL_FILE, JournalEvent))
    except (OSError, ValueError):
        return None


# --- Учтённые сообщения ------------------------------------------------------------------------------------


def recorded_ids(cost_bytes: bytes) -> set[str]:
    """id сообщений, уже записанных в `cost.jsonl` (по всем сессиям). Непонятная строка не мешает."""
    ids: set[str] = set()
    for raw in cost_bytes.split(b"\n"):
        if b"message_id" not in raw:
            continue
        try:
            value = json.loads(raw).get("message_id")
        except (ValueError, AttributeError):
            continue
        if isinstance(value, str):
            ids.add(value)
    return ids


def _record_line(
    u: tr.Usage,
    usd: float | None,
    prices: budget.Prices,
    *,
    session_id: str,
    role: str,
    step: str,
    when: Any,
) -> bytes:
    searches = pricing.web_search_count(u)
    web_usd = pricing.web_search_cost(prices, u) if usd is not None and searches else None
    record = CostRecord(
        schema_version=SCHEMA_VERSION,
        time=when,
        session_id=session_id,
        role=role,
        model=u.model,
        environment=ENVIRONMENT,
        step=step,  # type: ignore[arg-type]
        entry="по сообщению",
        message_id=u.message_id,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_write_5m_tokens=u.cache_write_5m_tokens,
        cache_write_1h_tokens=u.cache_write_1h_tokens,
        cache_read_tokens=u.cache_read_tokens,
        web_searches=searches,
        web_search_usd=web_usd,
        cost_usd=usd,
        cost_source="оценка" if usd is not None else "нет данных",
    )
    return json.dumps(record.model_dump(mode="json", exclude_none=True), ensure_ascii=False).encode("utf-8")


def _step_of(state_bytes: bytes) -> str:
    state = yaml.safe_load(state_bytes.decode("utf-8-sig"))
    step = state.get("step") if isinstance(state, dict) else None
    if not isinstance(step, str):
        raise ValueError("в state.yaml нет шага")
    return step


# --- Учёт --------------------------------------------------------------------------------------------------


def account(
    root: Path,
    session_id: str,
    sources: Sequence[Source],
    *,
    prices: budget.Prices | None = None,
    timeout: float | None = None,
) -> Result:
    """Записывает в `cost.jsonl` сообщения, которых там ещё нет. Сбой — исключение: решает вызывающий."""
    seconds = storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT) if timeout is None else timeout
    prices = prices or budget.load_prices()
    result = Result()
    with storage.project_lock(root, seconds):
        state_bytes = (root / storage.STATE_FILE).read_bytes()
        step = _step_of(state_bytes)
        saved = load_offsets(root)
        seen = recorded_ids((root / storage.COST_FILE).read_bytes())
        when = proj.now()
        cut = copy_cut(root)
        lines: list[bytes] = []
        for src in sources:
            if not src.path.is_file():
                if src.required:
                    raise FileNotFoundError(f"транскрипт не найден: {src.key}")
                continue
            entry = saved["files"].get(src.key)
            old_skipped = entry.get("skipped", 0) if isinstance(entry, dict) else 0
            chunk = tr.read_new(src.path, tr.Cursor.from_dict(entry))
            parsed = tr.parse(chunk.data)
            result.skipped += parsed.skipped
            for message_id, usage in parsed.messages.items():
                if message_id in seen:
                    result.duplicates += 1
                    continue
                if cut is not None and usage.time is not None and usage.time < cut:
                    result.earlier += 1
                    continue
                seen.add(message_id)
                usd = pricing.message_cost(prices, usage)
                if usd is None:
                    if usage.model not in result.unpriced:
                        result.unpriced.append(usage.model)
                else:
                    result.usd += usd
                lines.append(
                    _record_line(
                        usage, usd, prices, session_id=session_id, role=src.role, step=step, when=when
                    )
                )
            keep = 0 if chunk.restarted else (old_skipped if isinstance(old_skipped, int) else 0)
            saved["files"][src.key] = {**chunk.cursor.as_dict(), "skipped": keep + parsed.skipped}
        if lines:
            storage.append_tracked(root, storage.COST_FILE, lambda _s, _c: b"\n".join(lines), seconds)
            result.added = len(lines)
        saved[offsets.RECOUNTED_KEY] = offsets.wall()
        exhausted = _exhausted(root, seconds)
        if exhausted is not None:
            saved[offsets.EXHAUSTED_KEY] = exhausted
        else:
            saved.pop(offsets.EXHAUSTED_KEY, None)
        _save_offsets(root, saved, seconds)
    return result


# --- Транскрипты события хука ------------------------------------------------------------------------------


def _text(event: Mapping[str, Any], key: str) -> str | None:
    value = event.get(key)
    return value if isinstance(value, str) and value.strip() else None


def agents_dir(transcript_path: str) -> Path:
    """Папка транскриптов субагентов сессии: рядом с основным транскриптом, `<сессия>/subagents/`."""
    main = Path(transcript_path)
    return main.with_suffix("") / "subagents"


def agent_file_id(path: Path) -> str | None:
    name = path.name
    if name.startswith("agent-") and name.endswith(".jsonl"):
        return name[len("agent-") : -len(".jsonl")] or None
    return None


def _agent_role_from_meta(path: Path) -> str | None:
    """Роль субагента по соседнему `agent-<id>.meta.json` (`agentType`); нет файла или поля — None."""
    meta = path.with_name(path.name[: -len(".jsonl")] + ".meta.json")
    try:
        value = json.loads(meta.read_bytes().decode("utf-8")).get("agentType")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def _session_key(event: Mapping[str, Any]) -> str:
    return _text(event, "session_id") or "-"


def own_agent_source(event: Mapping[str, Any], *, required: bool = True) -> Source | None:
    """Транскрипт субагента, от имени которого пришло событие (`SubagentStop`, вызов в субагенте)."""
    agent_id = _text(event, "agent_id")
    if agent_id is None:
        return None
    given = _text(event, "agent_transcript_path")
    transcript = _text(event, "transcript_path")
    if given is not None:
        path = Path(given)
    elif transcript is not None:
        path = agents_dir(transcript) / f"agent-{agent_id}.jsonl"
    else:
        return None
    role = _text(event, "agent_type") or INTERNAL_ROLE
    return Source(f"agent:{agent_id}", path, role, required)


def sources_for(
    event: Mapping[str, Any], *, main: bool, sweep: bool, own_required: bool = True
) -> list[Source]:
    """Транскрипты для разбора по событию хука.

    `main` — основной транскрипт сессии (`transcript_path`); `sweep` — ещё транскрипты других субагентов
    сессии, у которых роль известна по `agent-<id>.meta.json` (чужую роль не угадываем). Транскрипт самого
    субагента событием называется всегда, если событие пришло из субагента.
    """
    out: list[Source] = []
    transcript = _text(event, "transcript_path")
    if main and transcript is not None:
        # смещения у каждой сессии свои: параллельные сессии проекта не сбивают друг другу чтение с места
        out.append(Source(f"main:{_session_key(event)}", Path(transcript), MAIN_ROLE))
    own = own_agent_source(event, required=own_required)
    if own is not None:
        out.append(own)
    if sweep and transcript is not None:
        folder = agents_dir(transcript)
        taken = {s.key for s in out}
        found = sorted(folder.glob("agent-*.jsonl")) if folder.is_dir() else []
        for path in found:
            agent_id = agent_file_id(path)
            if agent_id is None or f"agent:{agent_id}" in taken:
                continue
            role = _agent_role_from_meta(path)
            if role is not None:
                out.append(Source(f"agent:{agent_id}", path, role, required=False))
    return out
