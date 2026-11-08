"""Разбор транскрипта Claude Code для учёта стоимости (ADR-0010): чистые функции без файлов и сети.

Транскрипт — JSONL: на каждую строку один объект. Формат внутренний и меняется между версиями среды, поэтому
разбор берёт только то, что нужно учёту, и ничего не делает с текстом разговора. Служебные сообщения среды
(`model: <synthetic>`) не стоят денег и в пропуски не входят. Из сообщения ассистента
(`type: assistant`) берутся:

- `message.id` — ключ против удвоения: сообщение с несколькими блоками (мысль, текст, вызов инструмента)
  записывается несколькими строками с одним id и одним `usage`;
- `message.model` — id модели, как в таблице цен;
- `message.usage` — токены ввода и вывода, чтения кэша, записи кэша (`cache_creation_input_tokens` — сумма,
  `cache_creation.ephemeral_5m_input_tokens` и `ephemeral_1h_input_tokens` — разбивка), число веб-поисков
  (`server_tool_use.web_search_requests`), регион вывода `inference_geo` и режим `speed`.

Всё остальное (текст, вызовы инструментов, результаты) разбор не читает и не хранит. Строка, которую нельзя
разобрать, и сообщение без `usage`, id или модели пропускаются и считаются в `skipped`; хук не падает.

Чтение с места: `read_new` отдаёт только полные строки после сохранённого смещения. Файл короче смещения или
с другим началом (его переписали) читается с начала; повторный разбор не удваивает расход, потому что учёт
идёт по id сообщения (см. `strateg.accounting`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HEAD_BYTES = 1024  # по началу файла замечают, что его переписали
SYNTHETIC_MODEL = "<synthetic>"  # служебные сообщения среды (ошибка API, прерывание) без `usage`


@dataclass(frozen=True)
class Usage:
    """Учётные поля одного сообщения ассистента: только числа, id и модель."""

    message_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    geo: str | None = None  # `inference_geo`: "us" — ответ из США (цены ×1,1)
    speed: str | None = None  # "fast" — быстрый режим

    def merged(self, other: Usage) -> Usage:
        """Повтор того же сообщения: числа не убывают, берётся большее (вывод растёт по ходу ответа)."""
        return Usage(
            message_id=self.message_id,
            model=other.model or self.model,
            input_tokens=max(self.input_tokens, other.input_tokens),
            output_tokens=max(self.output_tokens, other.output_tokens),
            cache_write_5m_tokens=max(self.cache_write_5m_tokens, other.cache_write_5m_tokens),
            cache_write_1h_tokens=max(self.cache_write_1h_tokens, other.cache_write_1h_tokens),
            cache_read_tokens=max(self.cache_read_tokens, other.cache_read_tokens),
            web_searches=max(self.web_searches, other.web_searches),
            geo=other.geo or self.geo,
            speed=other.speed or self.speed,
        )


@dataclass
class Parsed:
    """Итог разбора куска транскрипта."""

    messages: dict[str, Usage] = field(default_factory=dict)  # по id, в порядке появления
    skipped: int = 0  # битые строки и сообщения без usage, id или модели
    lines: int = 0  # сколько непустых строк просмотрено


def _count(value: Any) -> int | None:
    """Целое неотрицательное число токенов; всё остальное (строка, дробь, bool, минус) — None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _field(source: Mapping[str, Any], key: str, *, required: bool = False) -> int | None:
    """Число из словаря; нет ключа — 0 (или None, если поле обязательно); не число — None."""
    if key not in source:
        return None if required else 0
    return _count(source[key])


def usage_of(message: Mapping[str, Any]) -> Usage | None:
    """Учётные поля сообщения ассистента; None — сообщение нельзя учесть."""
    message_id, model, usage = message.get("id"), message.get("model"), message.get("usage")
    if not isinstance(message_id, str) or not message_id or not isinstance(model, str) or not model:
        return None
    if model == SYNTHETIC_MODEL or not isinstance(usage, dict):
        return None
    tokens_in = _field(usage, "input_tokens", required=True)
    tokens_out = _field(usage, "output_tokens", required=True)
    cache_read = _field(usage, "cache_read_input_tokens")
    cache_total = _field(usage, "cache_creation_input_tokens")
    if None in (tokens_in, tokens_out, cache_read, cache_total):
        return None
    write_5m, write_1h = 0, 0
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        a, b = _field(split, "ephemeral_5m_input_tokens"), _field(split, "ephemeral_1h_input_tokens")
        if a is None or b is None:
            return None
        write_5m, write_1h = a, b
    # Сумма важнее разбивки: что в разбивку не вошло, считается записью на 5 минут (обычный срок кэша).
    write_5m += max((cache_total or 0) - write_5m - write_1h, 0)
    searches = 0
    server = usage.get("server_tool_use")
    if isinstance(server, dict):
        found = _field(server, "web_search_requests")
        if found is None:
            return None
        searches = found
    geo, speed = usage.get("inference_geo"), usage.get("speed")
    return Usage(
        message_id=message_id,
        model=model,
        input_tokens=tokens_in or 0,
        output_tokens=tokens_out or 0,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=cache_read or 0,
        web_searches=searches,
        geo=geo if isinstance(geo, str) else None,
        speed=speed if isinstance(speed, str) else None,
    )


def parse(data: bytes) -> Parsed:
    """Разбирает байты транскрипта (целые строки). Не бросает исключений на любых входных данных."""
    out = Parsed()
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        out.lines += 1
        if not (line.startswith(b"{") and line.endswith(b"}")):
            out.skipped += 1  # оборванная или чужая строка
            continue
        if b'"assistant"' not in line:
            continue  # пользовательские строки, вызовы API, вложения: учёту они не нужны
        try:
            record = json.loads(line)
        except ValueError:
            out.skipped += 1
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if isinstance(message, dict) and message.get("model") == SYNTHETIC_MODEL:
            continue  # служебное сообщение среды (ошибка API, прерывание): не деньги и не повреждение
        found = usage_of(message) if isinstance(message, dict) else None
        if found is None:
            out.skipped += 1
            continue
        before = out.messages.get(found.message_id)
        out.messages[found.message_id] = found if before is None else before.merged(found)
    return out


# --- Чтение с места ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cursor:
    """Где остановился разбор файла: смещение и отпечаток начала файла."""

    offset: int = 0
    head_len: int = 0
    head_sha: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"offset": self.offset, "head_len": self.head_len, "head_sha": self.head_sha}

    @classmethod
    def from_dict(cls, raw: Any) -> Cursor:
        if not isinstance(raw, dict):
            return cls()
        offset, head_len, head_sha = raw.get("offset"), raw.get("head_len"), raw.get("head_sha")
        if _count(offset) is None or _count(head_len) is None or not isinstance(head_sha, str):
            return cls()
        return cls(offset, head_len, head_sha)


@dataclass(frozen=True)
class Chunk:
    data: bytes  # целые строки после смещения
    cursor: Cursor  # куда встанет разбор, если эти строки учтены
    restarted: bool = False  # файл обрезан или переписан: чтение началось с начала


def _head(path: Path, length: int) -> bytes:
    with path.open("rb") as f:
        return f.read(length)


def read_new(path: Path, cursor: Cursor) -> Chunk:
    """Читает строки, появившиеся после `cursor`.

    Нет файла — `FileNotFoundError`. Последняя строка без перевода строки берётся, только если она целый
    объект JSON: иначе её ещё пишут, и она достанется следующему разбору.
    """
    size = path.stat().st_size
    offset, restarted = cursor.offset, False
    if offset > 0:
        rewritten = size < offset or (
            cursor.head_len > 0
            and hashlib.sha256(_head(path, cursor.head_len)).hexdigest() != cursor.head_sha
        )
        if rewritten:
            offset, restarted = 0, True
    with path.open("rb") as f:
        f.seek(offset)
        raw = f.read()
    end = raw.rfind(b"\n") + 1
    tail = raw[end:].strip()
    if tail:
        try:
            ok = isinstance(json.loads(tail), dict)
        except ValueError:
            ok = False
        if ok:
            end = len(raw)
    data = raw[:end]
    new_offset = offset + end
    head_len = min(new_offset, HEAD_BYTES)
    head_sha = hashlib.sha256(_head(path, head_len)).hexdigest() if head_len else ""
    return Chunk(data, Cursor(new_offset, head_len, head_sha), restarted)
