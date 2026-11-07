"""Запись файлов выхода (рамка, план, пробелы, варианты, обещания, план проверки, решения) командой `write`.

Вход — словарь «вид -> содержимое файла без `schema_version`». Здесь разбор и проверка входа моделями,
сравнение с записанным, версии в `history/` и события журнала. Откат при новых ошибках проверки проекта
делает `project.transaction` в команде.

Рамка до первого ответа на В1 — черновик: её перезаписывают на месте, без новой версии и без `history/`.
После ответа на В1 каждая правка рамки — новая версия: прежняя ложится в `history/frame.v<N>.yaml`,
`version` растёт на единицу. Остальные файлы выхода при перезаписи всегда кладут прежний в `history/`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from strateg import project as proj
from strateg.models import (
    MAX_FRAME_VERSION,
    SCHEMA_VERSION,
    DecisionsFile,
    FrameFile,
    GapsFile,
    JournalEvent,
    OptionsFile,
    PlanFile,
    PromisesFile,
    VerificationFile,
)
from strateg.project import CliError, Project, UsageError
from strateg.project_check import (
    DECISIONS_FILE,
    FRAME_FILE,
    GAPS_FILE,
    HISTORY_DIR,
    OPTIONS_FILE,
    PLAN_FILE,
    PROMISES_FILE,
    VERIFICATION_FILE,
    _format_errors,
)


@dataclass(frozen=True)
class Kind:
    """Вид файла выхода: имя во входе, файл, модель и разделы-списки (для подписи записи в ошибке)."""

    name: str
    file: str
    model: type[BaseModel]
    sections: tuple[str, ...]


KINDS: dict[str, Kind] = {
    k.name: k
    for k in (
        Kind("frame", FRAME_FILE, FrameFile, ()),
        Kind("plan", PLAN_FILE, PlanFile, ("questions",)),
        Kind("gaps", GAPS_FILE, GapsFile, ("gaps", "metric_coverage")),
        Kind("options", OPTIONS_FILE, OptionsFile, ("options",)),
        Kind("promises", PROMISES_FILE, PromisesFile, ("promises",)),
        Kind("verification", VERIFICATION_FILE, VerificationFile, ("rows", "premortem", "undesired_effects")),
        Kind("decisions", DECISIONS_FILE, DecisionsFile, ("decisions",)),
    )
}
SERVICE_HINTS = {
    "schema_version": "версию схемы ставит CLI",
    "version": "номер версии рамки ставит CLI: первая запись — 1, после ответа на В1 она растёт на единицу",
}


@dataclass
class Change:
    """Что `write` сделает с одним файлом."""

    kind: Kind
    model: BaseModel
    text: str
    old: bytes | None  # прежнее содержимое файла
    history: str | None  # куда ляжет прежнее содержимое
    version: int | None  # версия рамки
    note: str  # как записано: для журнала и вывода


# ---------------------------------------------------------------------------
# Разбор и проверка входа
# ---------------------------------------------------------------------------


def parse_input(raw: Any) -> dict[str, dict[str, Any]]:
    """Словарь «вид -> содержимое». Ошибка входа — `CliError`, служебное поле во входе — `UsageError`."""
    names = ", ".join(KINDS)
    if not isinstance(raw, dict) or not raw:
        raise CliError(
            "во входе нужен набор «вид: содержимое файла»",
            [f"допустимые виды: {names}; пример: `frame:` и под ним поля рамки"],
        )
    errors: list[str] = []
    for key, content in raw.items():
        if key not in KINDS:
            errors.append(f"вид «{key}» не бывает: допустимо {names}")
        elif not isinstance(content, dict):
            errors.append(f"вид {key}: ожидается набор полей «ключ: значение»")
    if errors:
        raise CliError("файлы выхода не записаны: вход не по схеме", errors)
    for key, content in raw.items():
        for name, hint in SERVICE_HINTS.items():
            if name in content and (name != "version" or key == "frame"):
                raise UsageError(f"вид {key}: поле {name} во входе не пишут: {hint}")
    return raw


def validate(kind: Kind, content: dict[str, Any]) -> tuple[BaseModel | None, list[str]]:
    """Содержимое проверяется моделью файла до записи. Сообщения — как у `check`: файл, запись, поле."""
    data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, **content}
    if kind.name == "frame":
        data["version"] = 1  # настоящую версию ставит CLI при записи
    try:
        return kind.model.model_validate(data, by_name=False), []
    except ValidationError as exc:
        return None, _format_errors(kind.file, kind.sections, data, exc)


# ---------------------------------------------------------------------------
# Что менять
# ---------------------------------------------------------------------------


def b1_answered(journal: list[JournalEvent]) -> bool:
    """Был ли когда-нибудь ответ на В1: с него рамка перестаёт быть черновиком."""
    return any(e.gate is not None and e.gate.gate == "В1" and e.gate.answered_at is not None for e in journal)


def _dump(model: BaseModel, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    data = model.model_dump(mode="json", exclude_none=True)
    for name in drop:
        data.pop(name, None)
    return data


def _read_old(p: Project, kind: Kind) -> tuple[bytes | None, BaseModel | None]:
    """Прежний файл: содержимое и разобранная модель (None, если файла нет или он не разбирается)."""
    path = p.root / kind.file
    if not path.is_file():
        return None, None
    old = path.read_bytes()
    try:
        raw = proj.read_yaml(path)
        return old, kind.model.model_validate(raw, by_name=False)
    except (CliError, ValidationError):
        return old, None


def _next_history_path(p: Project, file: str) -> str:
    """Следующий свободный номер версии файла в `history/`."""
    taken = 0
    folder = p.root / HISTORY_DIR
    if folder.is_dir():
        for path in folder.rglob("*"):
            source = proj.history_source(path.relative_to(p.root).as_posix()) if path.is_file() else None
            if source is not None and source[0] == file:
                taken = max(taken, source[1])
    return proj.history_path(file, taken + 1)


def plan_changes(p: Project, models: dict[str, BaseModel]) -> tuple[list[Change], list[Kind]]:
    """Изменённые файлы и виды, которые остались без изменений (после разбора моделью содержимое то же)."""
    changes: list[Change] = []
    same: list[Kind] = []
    for name, model in models.items():
        kind = KINDS[name]
        old_bytes, old_model = _read_old(p, kind)
        version: int | None = None
        history: str | None = None
        drop = ("version",) if name == "frame" else ()
        if old_model is not None and _dump(old_model, drop=drop) == _dump(model, drop=drop):
            same.append(kind)
            continue
        if name == "frame":
            assert isinstance(model, FrameFile)
            if old_bytes is None:
                version, note = 1, "создан, версия 1"
            elif not b1_answered(p.journal):
                version = old_model.version if isinstance(old_model, FrameFile) else 1
                note = f"черновик перезаписан, версия {version} (до ответа на В1 версии не растут)"
            else:
                if not isinstance(old_model, FrameFile):
                    raise CliError(
                        f"{FRAME_FILE} не читается, версию рамки не определить: запустите `strateg check`"
                    )
                version = old_model.version + 1
                history = proj.history_path(FRAME_FILE, old_model.version)
                if (p.root / history).exists():
                    raise CliError(
                        f"{history} уже есть, а рамка версии {old_model.version}: версии рамки разошлись, "
                        "запустите `strateg check`"
                    )
                note = f"перезаписан после ответа на В1: версия {version}, прежняя в {history}"
            if version > MAX_FRAME_VERSION:
                raise CliError(f"версия рамки {version} больше предела {MAX_FRAME_VERSION}")
            model = model.model_copy(update={"version": version})
        elif old_bytes is None:
            note = "создан"
        else:
            history = _next_history_path(p, kind.file)
            note = f"перезаписан, прежняя версия в {history}"
        changes.append(Change(kind, model, proj.dump_yaml(model), old_bytes, history, version, note))
    return changes, same


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------


def _writes_so_far(p: Project, kind: Kind) -> int:
    name = f"strateg write {kind.name}"
    return sum(1 for e in p.journal if e.kind == "действие" and e.name == name)


def apply_change(p: Project, ch: Change, started: float, by: str) -> None:
    """Кладёт прежнюю версию в `history/`, пишет файл и событие журнала."""
    if ch.history is not None and ch.old is not None:
        dest = p.root / ch.history
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(ch.old)  # байт в байт, как лежал
        tmp.replace(dest)
    proj.write_text(p.root / ch.kind.file, ch.text)
    message = f"{ch.kind.file}: {ch.note}"
    p.add_event(
        p.event(
            kind="действие",
            name=f"strateg write {ch.kind.name}",
            # номер записи в ключе: тот же текст можно вернуть после другого, и это новая запись
            idempotency_hash=proj.key_hash(
                "write",
                ch.kind.name,
                _writes_so_far(p, ch.kind) + 1,
                proj.sha256_bytes(ch.text.encode("utf-8")),
            ),
            status="выполнено",
            duration_s=round(time.monotonic() - started, 2),
            message=message,
        )
    )
    if by == "пользователь":
        p.add_event(p.event(kind="исправление", actor="пользователь", message=f"правка человека: {message}"))


_MISSING_FILE = re.compile(r"но файла (\S+\.yaml) нет")
_MISSING_ID = re.compile(r"такого номера нет в (\S+\.yaml)")


def add_hints(details: list[str], written: list[str]) -> list[str]:
    """К ошибке про ссылку на ненаписанный файл добавляет подсказку: взаимные ссылки пишут одним входом."""
    out = []
    for line in details:
        m = _MISSING_FILE.search(line) or _MISSING_ID.search(line)
        if m and m.group(1) not in written:
            line += (
                f" — файл {m.group(1)} не записан или в нём нет этого номера: файлы, которые ссылаются "
                "друг на друга, записывают одним входом (`strateg write` с двумя видами сразу)"
            )
        out.append(line)
    return out
