"""Работа с папкой проекта: выбор активного проекта, чтение и запись файлов, журнал, время и стоимость.

Все файлы пишутся через модели из `strateg.models` (UTF-8 без BOM, окончания LF), состояние и реестры —
целиком через временный файл, журналы — только добавлением строки. После изменения хеши файлов
попадают в `state.yaml` (поле `files`), а `transaction` откатывает изменение, если проверка проекта
нашла новые ошибки.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from strateg.models import (
    SCHEMA_VERSION,
    Budget,
    ClaimsFile,
    CostRecord,
    JournalEvent,
    ProjectState,
    SourcesFile,
)
from strateg.project_check import (
    CLAIMS_FILE,
    COST_FILE,
    JOURNAL_FILE,
    SOURCES_FILE,
    STATE_FILE,
    check_project,
)

ACTIVE_FILE = ".active"
PROJECTS_ENV = "STRATEG_PROJECTS_DIR"  # где лежат проекты (по умолчанию — projects/ репозитория)
PROJECT_ENV = "STRATEG_PROJECT"  # активный проект: номер или путь к папке
DEFAULT_LIMIT_MINUTES = 60
SUBFOLDERS = ("inputs", "sources", "outputs", ".tmp")  # .tmp: временные файлы агента, вне проверки
CACHE_DIR = ".cache"

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class CliError(Exception):
    """Ошибка данных или правил (код возврата 1). Текст — по-русски, для человека."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []


class UsageError(CliError):
    """Ошибка использования (код возврата 2)."""


def now() -> dt.datetime:
    """Текущее время с поясом. Отдельная функция, чтобы тесты подменяли часы."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key_hash(*parts: object) -> str:
    """Ключ идемпотентности: хеш команды и входов."""
    payload = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# Где лежат проекты
# ---------------------------------------------------------------------------


def projects_root() -> Path:
    env = os.environ.get(PROJECTS_ENV)
    if env:
        return Path(env)
    from strateg.cli import find_repo_root  # импорт здесь: cli импортирует этот модуль

    repo = find_repo_root()
    if repo is None:
        raise CliError(
            "не найден корень репозитория (pyproject.toml); задайте папку проектов в " + PROJECTS_ENV
        )
    return repo / "projects"


def check_project_id(project_id: str) -> str:
    if not _PROJECT_ID.match(project_id):
        raise UsageError(
            f"номер проекта «{project_id}» не подходит: нужны латинские строчные буквы, цифры, «-» и «_», "
            "начинаться надо с буквы или цифры"
        )
    return project_id


def resolve_project(explicit: str | None = None) -> Path:
    """Папка активного проекта: `--project`, затем STRATEG_PROJECT, затем `projects/.active`."""
    choice = explicit or os.environ.get(PROJECT_ENV)
    if choice:
        looks_like_path = any(sep in choice for sep in ("/", "\\")) or Path(choice).is_absolute()
        root = Path(choice) if looks_like_path else projects_root() / check_project_id(choice)
    else:
        active = projects_root() / ACTIVE_FILE
        if not active.is_file():
            raise CliError("активный проект не выбран: выполните `strateg use <id>` или `strateg new <id>`")
        name = active.read_text(encoding="utf-8").strip()
        root = projects_root() / check_project_id(name)
    if not (root / STATE_FILE).is_file():
        raise CliError(f"проект не найден: {root.name} (нет файла {STATE_FILE})")
    return root


def set_active(project_id: str) -> None:
    folder = projects_root()
    folder.mkdir(parents=True, exist_ok=True)
    write_text(folder / ACTIVE_FILE, project_id + "\n")


# ---------------------------------------------------------------------------
# Чтение и запись файлов
# ---------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    """UTF-8 без BOM, окончания LF, замена файла целиком."""
    data = text.replace("\r\n", "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def dump_yaml(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100)


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CliError(f"файл {path.name} не читается: запустите `strateg check`") from exc
    if not isinstance(data, dict):
        raise CliError(f"файл {path.name} пуст или не набор «ключ: значение»: запустите `strateg check`")
    return data


def append_jsonl(path: Path, model: BaseModel) -> None:
    line = json.dumps(model.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    with path.open("ab") as f:
        f.write(line.encode("utf-8") + b"\n")


def read_jsonl(path: Path, model: type[BaseModel]) -> list[Any]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_bytes().decode("utf-8").split("\n"):
        if line.strip():
            out.append(model.model_validate(json.loads(line)))
    return out


@dataclass
class Project:
    """Открытый проект: папка и разобранные файлы."""

    root: Path
    state: ProjectState
    sources: SourcesFile
    claims: ClaimsFile
    journal: list[JournalEvent] = field(default_factory=list)
    costs: list[CostRecord] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.state.id

    def path(self, rel: str) -> Path:
        return self.root / rel

    # --- чтение ---
    @classmethod
    def load(cls, root: Path) -> Project:
        try:
            return cls(
                root=root,
                state=ProjectState.model_validate(read_yaml(root / STATE_FILE)),
                sources=SourcesFile.model_validate(read_yaml(root / SOURCES_FILE)),
                claims=ClaimsFile.model_validate(read_yaml(root / CLAIMS_FILE)),
                journal=read_jsonl(root / JOURNAL_FILE, JournalEvent),
                costs=read_jsonl(root / COST_FILE, CostRecord),
            )
        except (ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
            errors = check_project(root)
            raise CliError(
                "файлы проекта не читаются, исправьте ошибки и повторите",
                errors or [str(type(exc).__name__)],
            ) from exc

    # --- запись ---
    def save_state(self) -> None:
        self.state.files = self._hashes()
        write_text(self.root / STATE_FILE, dump_yaml(self.state))

    def save_sources(self) -> None:
        write_text(self.root / SOURCES_FILE, dump_yaml(self.sources))

    def save_claims(self) -> None:
        write_text(self.root / CLAIMS_FILE, dump_yaml(self.claims))

    def add_event(self, event: JournalEvent) -> None:
        append_jsonl(self.root / JOURNAL_FILE, event)
        self.journal.append(event)

    def add_cost(self, record: CostRecord) -> None:
        append_jsonl(self.root / COST_FILE, record)
        self.costs.append(record)

    def _hashes(self) -> dict[str, str]:
        rels = [SOURCES_FILE, CLAIMS_FILE, JOURNAL_FILE, COST_FILE]
        rels += [s.text_path for s in self.sources.sources if s.text_path]
        out = {}
        for rel in sorted(set(rels)):
            p = self.root / rel
            if p.is_file():
                out[rel] = sha256_bytes(p.read_bytes())
        return out

    def event(self, **kw: Any) -> JournalEvent:
        """Событие журнала с общими полями: версия схемы, время, исполнитель CLI, текущий шаг."""
        kw.setdefault("time", now())
        kw.setdefault("actor", "CLI")
        kw.setdefault("step", self.state.step)
        return JournalEvent(schema_version=SCHEMA_VERSION, **kw)


# ---------------------------------------------------------------------------
# Создание проекта
# ---------------------------------------------------------------------------


def create_project(project_id: str, mode: str, region: str | None) -> Project:
    root = projects_root() / check_project_id(project_id)
    if root.exists():
        raise CliError(f"проект {project_id} уже существует: {root.name}")
    root.mkdir(parents=True)
    for name in SUBFOLDERS:
        (root / name).mkdir()
    state = ProjectState(
        schema_version=SCHEMA_VERSION,
        id=project_id,
        mode=mode,  # type: ignore[arg-type]
        step="1",
        gates={"В1": "впереди", "В2": "впереди", "В3": "впереди", "В4": "впереди", "В5": "впереди"},
        budget=Budget(limit_minutes=DEFAULT_LIMIT_MINUTES, spent_minutes=0),
        region=region,
        next_action="Принять запрос и начать шаг 1: `strateg step start 1`",
    )
    write_text(root / SOURCES_FILE, dump_yaml(SourcesFile(schema_version=SCHEMA_VERSION)))
    write_text(root / CLAIMS_FILE, dump_yaml(ClaimsFile(schema_version=SCHEMA_VERSION)))
    write_text(root / JOURNAL_FILE, "")
    write_text(root / COST_FILE, "")
    project = Project(
        root=root,
        state=state,
        sources=SourcesFile(schema_version=SCHEMA_VERSION),
        claims=ClaimsFile(schema_version=SCHEMA_VERSION),
    )
    project.add_event(
        project.event(
            kind="действие",
            name="проект создан",
            idempotency_hash=key_hash("new", project_id),
            status="выполнено",
            duration_s=0,
        )
    )
    project.save_state()
    return project


# ---------------------------------------------------------------------------
# Откат при новых ошибках проверки
# ---------------------------------------------------------------------------


def _tracked_files(root: Path) -> list[Path]:
    names = [STATE_FILE, SOURCES_FILE, CLAIMS_FILE, JOURNAL_FILE, COST_FILE]
    files = [root / n for n in names]
    sources = root / "sources"
    if sources.is_dir():
        files += [p for p in sorted(sources.iterdir()) if p.is_file()]
    return files


@contextlib.contextmanager
def transaction(root: Path, *, rollback: bool = True) -> Iterator[None]:
    """Изменения внутри блока откатываются при исключении и при новых ошибках проверки проекта.

    С `rollback=False` откатывается только исключение; новые ошибки проверки сообщает сам вызывающий код.
    """
    before = {p: (p.read_bytes() if p.is_file() else None) for p in _tracked_files(root)}
    baseline = set(check_project(root))

    def restore() -> None:
        for p in _tracked_files(root):
            if p not in before:
                p.unlink(missing_ok=True)
        for p, data in before.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, p)

    try:
        yield
    except BaseException:
        restore()
        raise
    if rollback:
        new = [e for e in check_project(root) if e not in baseline]
        if new:
            restore()
            raise CliError("проверка проекта нашла ошибки, изменения не записаны", new)


# ---------------------------------------------------------------------------
# Время работы и стоимость
# ---------------------------------------------------------------------------


def step_action_name(step: str) -> str:
    return f"шаг {step}"


def gate_waits(events: list[JournalEvent]) -> list[tuple[dt.datetime, dt.datetime | None]]:
    """Ожидание ответа на воротах: открытие и ответ (None — ответа ещё нет).

    Ворота без вопросов человеку пройдены сразу, ожидания в них нет.
    """
    waits: dict[tuple[str, dt.datetime], dt.datetime | None] = {}
    for e in events:
        g = e.gate
        if g is None or g.questions == 0:
            continue
        key = (g.gate, g.opened_at)
        if g.answered_at is not None or key not in waits:
            waits[key] = g.answered_at
    return [(opened, answered) for (_, opened), answered in waits.items()]


def overlap_seconds(a0: dt.datetime, a1: dt.datetime, b0: dt.datetime, b1: dt.datetime) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max((hi - lo).total_seconds(), 0.0)


def work_seconds(events: list[JournalEvent], at: dt.datetime | None = None) -> float:
    """Время работы: сумма длительностей шагов, записанных при `step done`.

    С `at` к сумме добавляется идущий шаг (начат, не завершён) без ожидания на воротах.
    """
    total = sum(
        e.duration_s or 0.0
        for e in events
        if e.kind == "действие" and e.status == "выполнено" and (e.name or "").startswith("шаг ")
    )
    if at is not None:
        last: dict[str, JournalEvent] = {}
        for e in events:
            if e.kind == "действие" and (e.name or "").startswith("шаг ") and e.idempotency_hash:
                last[e.idempotency_hash] = e
        waits = gate_waits(events)
        for e in last.values():
            if e.status == "начато":
                waited = sum(
                    overlap_seconds(e.time, at, opened, answered or at) for opened, answered in waits
                )
                total += max((at - e.time).total_seconds() - waited, 0.0)
    return total


def cost_total(records: list[CostRecord]) -> tuple[float | None, str, bool]:
    """Стоимость проекта: (сумма USD или None, точность, полнота).

    По каждой сессии берётся последний «итог сессии», а если его нет — сумма записей «по сообщению»;
    сессии складываются (ADR-0010). Точность — «измерено», только если все записи измерены.
    """
    by_session: dict[str, list[CostRecord]] = {}
    for r in records:
        by_session.setdefault(r.session_id, []).append(r)
    total = 0.0
    known = False
    complete = True
    sources: set[str] = set()
    for recs in by_session.values():
        priced_totals = [i for i, r in enumerate(recs) if r.entry == "итог сессии" and r.cost_usd is not None]
        messages = [r for r in recs if r.entry == "по сообщению"]
        if priced_totals:
            k = priced_totals[-1]
            counted = [recs[k]] + [r for r in recs[k + 1 :] if r.entry == "по сообщению"]
        else:
            counted = messages
        if not counted:
            complete = False  # у сессии только итог без цены
        for r in counted:
            if r.cost_usd is None:
                complete = False
            else:
                known = True
                total += r.cost_usd
                sources.add(r.cost_source)
    if not known:
        return None, "нет данных", False
    precision = "измерено" if sources == {"измерено"} else "оценка"
    return round(total, 6), precision, complete
