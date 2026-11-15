"""Всё, что хуки берут у проекта и пишут в него: контекст, сведения о проекте и маршрутах, события журнала.

Тяжёлые модули (`project`, `routing`, `pack`) подключаются внутри функций: хук прав на каждый вызов
инструмента должен стартовать быстро. Проект нужен ему только для запуска субагента; отказ и другие
события он пишет в журнал без моделей проекта (`append_event`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from strateg import storage
from strateg.hooks import paths
from strateg.hooks.rules import Choice, Ctx, EventSpec, HookError, Pick, ProjectView, RoutingView, Services

ACTOR = "хук"
ONCE_PER_SESSION = "граница прав не активна"  # пишется раз за сессию: имя события — номер сессии
# События, которые пишутся один раз на своё имя: у «учёт недоступен» имя — номер сессии или «нет цены:
# модель». Проверка под блокировкой: параллельные хуки пишут событие один раз.
ONCE_KINDS = frozenset({ONCE_PER_SESSION, "учёт недоступен"})
# «Роль выполнена» — след завершения субагента (`SubagentStop`). Имя — id субагента; возобновлённый
# субагент завершается снова с тем же id, и это новое событие (`<id>#2`, `<id>#3`). Повтор того же хука
# (параллельный или повторная доставка) приходит в ту же секунду и события не добавляет.
ROLE_DONE = "роль выполнена"
ROLE_DONE_REPEAT_SECONDS = 3.0
# «Ответ человека» — след ответа на «внеплановую остановку» (`UserPromptSubmit`). Пишется, пока проект ждёт
# этого ответа (проверка под блокировкой): повторное сообщение человека второго события не даёт.
HUMAN_ANSWER = "ответ человека"
STOP_NAME = "внеплановая остановка"
TASK_KIND = "задание исследователю"
# Виды событий журнала (`models.EventKind`): строка хвоста с другим видом — не событие; тест сверяет списки
_EVENT_KINDS = frozenset(
    {
        "действие",
        "ворота",
        "ошибка",
        "исправление",
        "смена режима",
        "выбор модели",
        "изменено вне агента",
        "найдена инструкция в источнике",
        "бюджет",
        "учёт недоступен",
        "отказ хука",
        "граница прав не активна",
        "задание исследователю",
        "роль выполнена",
        "ответ человека",
    }
)
MAX_ERROR_CHARS = 500
HIDDEN_LINE = "[строка скрыта: похожа на ключ]"
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
JOURNAL, STATE = "journal.jsonl", "state.yaml"


def product_root(env: Mapping[str, str], cwd: str) -> str:
    """Корень продукта: `CLAUDE_PROJECT_DIR`, затем папка этого пакета (с `pyproject.toml`), затем `cwd`."""
    given = env.get("CLAUDE_PROJECT_DIR")
    if given:
        return given
    here = Path(__file__).resolve().parents[3]
    return str(here) if (here / "pyproject.toml").is_file() else cwd


def make_ctx(event: Mapping[str, Any], env: Mapping[str, str]) -> Ctx:
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else os.getcwd()
    root = product_root(env, cwd)
    projects = env.get("STRATEG_PROJECTS_DIR") or str(Path(root) / "projects")
    if not os.path.isabs(projects):
        projects = str(Path(cwd) / projects)
    scratch = event.get("scratchpad_dir")
    penv = paths.penv_from_system(
        root=root,
        projects=projects,
        cwd=cwd,
        env=env,
        scratchpad=scratch if isinstance(scratch, str) and scratch else None,
    )
    return Ctx(penv=penv, env=env)


# --- Проект и таблица маршрутов (подключают тяжёлые модули) -------------------------------------------------


def _plan_ids(root: Path) -> frozenset[str]:
    import yaml

    path = root / "plan.yaml"
    if not path.is_file():
        return frozenset()
    try:
        data = yaml.safe_load(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return frozenset()
    questions = data.get("questions") if isinstance(data, dict) else None
    ids = (q.get("id") for q in questions if isinstance(q, dict)) if isinstance(questions, list) else ()
    return frozenset(i for i in ids if isinstance(i, str))


def agent_names(agents_dir: Path) -> frozenset[str]:
    """Имена ролей по обёрткам `.claude/agents/*.md` (поле `name` в начале файла)."""
    import yaml

    names = set()
    for path in sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.DOTALL)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(meta, dict) and isinstance(meta.get("name"), str):
            names.add(meta["name"])
    return frozenset(names)


class LiveServices:
    """Проект и таблица маршрутов с диска. Читаются только когда решение их спросит."""

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self._view: tuple[ProjectView | None] | None = None

    def project(self) -> ProjectView | None:
        if self._view is None:
            self._view = (self._load(),)
        return self._view[0]

    def _load(self) -> ProjectView | None:
        from strateg import project as proj

        root = active_project_root(self.ctx)
        if root is None:
            return None
        p = proj.Project.load(root, timeout=storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT))
        picks = tuple(
            Pick(role=e.role, model=e.model, step=e.step, name=e.name or "")
            for e in p.journal
            if e.kind == "выбор модели" and e.role and e.model
        )
        return ProjectView(
            id=p.id,
            step=p.state.step,
            profile=p.state.profile or "обычный",
            picks=picks,
            plan_ids=_plan_ids(root),
        )

    def routing(self) -> RoutingView:
        from strateg import routing
        from strateg.project import CliError

        try:
            table = routing.load_routing()
        except CliError as exc:
            raise HookError(f"таблица маршрутов `config/routing.yaml` не читается: {exc.message}") from exc
        view = self.project()
        profile = view.profile if view else routing.DEFAULT_PROFILE
        roles: dict[str, Choice] = {}
        for role in table.roles:
            r = routing.effective(table, role, profile)
            roles[role] = Choice(level=r.level, model=r.model, effort=r.effort)
        return RoutingView(roles=roles)

    def agents(self) -> frozenset[str]:
        return agent_names(Path(self.ctx.penv.root) / ".claude" / "agents")

    def active(self) -> str | None:
        """Номер активного проекта без тяжёлых модулей (зоны чтения и записи ролей); нет проекта — None."""
        root = active_project_root(self.ctx)
        return root.name if root is not None else None

    def as_services(self) -> Services:
        return Services(project=self.project, routing=self.routing, agents=self.agents, active=self.active)


# --- Журнал ------------------------------------------------------------------------------------------------


def redact(text: str) -> str:
    """Строки, похожие на ключи, вырезаются целиком; остальное остаётся."""
    from strateg import pack

    return "\n".join(HIDDEN_LINE if pack.scan_text(line) else line for line in text.split("\n"))


def clip(text: str, limit: int) -> str:
    text = "".join(c if c in "\n\t" or ord(c) >= 32 else " " for c in text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def active_project_root(ctx: Ctx) -> Path | None:
    """Папка активного проекта: те же правила, что у `project.resolve_project`, но без тяжёлых импортов."""
    choice = ctx.env.get("STRATEG_PROJECT")
    projects = Path(ctx.penv.projects)
    if choice:
        looks_like_path = "/" in choice or "\\" in choice or Path(choice).is_absolute()
        if not looks_like_path and not PROJECT_ID.match(choice):
            return None
        root = Path(choice) if looks_like_path else projects / choice
    else:
        active = projects / ".active"
        if not active.is_file():
            return None
        name = active.read_bytes().decode("utf-8", errors="replace").strip()
        if not PROJECT_ID.match(name):
            return None
        root = projects / name
    return root if (root / STATE).is_file() and (root / JOURNAL).is_file() else None


def now() -> Any:
    """Текущее время с поясом, как у событий CLI. Отдельная функция: тесты подставляют часы проекта."""
    import datetime as dt

    return dt.datetime.now().astimezone().replace(microsecond=0)


def append_event(root: Path, spec: EventSpec, timeout: float | None = None) -> bool:
    """Добавляет событие в `journal.jsonl` проекта и обновляет в `state.yaml` только хеш журнала.

    Запись идёт тем же кодом, что и у CLI (`strateg.storage`): под блокировкой папки проекта, поэтому
    параллельные хуки и команда CLI не теряют событий и не дают ложного «изменено вне агента». Хеши других
    файлов не трогаются: правку человека принимает только команда CLI. Модели проекта не подключаются: хук
    на отказе должен отвечать быстро; правильность строки проверяют тесты (`check_project`). Возвращает
    False, если проект не удалось прочитать. Папка занята дольше таймаута (`timeout`, по умолчанию
    `storage.HOOK_LOCK_TIMEOUT`) — `storage.LockTimeout`.
    """
    import yaml

    def build(state_bytes: bytes, journal: bytes) -> bytes | None:
        if spec.kind in ONCE_KINDS and _noted(journal, spec):
            return None  # проверка под блокировкой: параллельные хуки одной сессии пишут событие один раз
        if spec.kind == HUMAN_ANSWER and not _waits_for_answer(journal):
            return None
        name = spec.name
        if spec.kind == ROLE_DONE:
            name = _role_done_name(journal, spec)
            if name is None:
                return None
        try:
            state = yaml.safe_load(state_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, yaml.YAMLError):
            return None
        if not isinstance(state, dict) or not isinstance(state.get("step"), str):
            return None
        event: dict[str, Any] = {
            "schema_version": 1,
            "time": now().isoformat(),
            "actor": ACTOR,
            "kind": spec.kind,
            "step": state["step"],
        }
        for key in ("name", "message", "role", "model", "level", "effort"):
            value = name if key == "name" else getattr(spec, key)
            if value is not None:
                event[key] = value
        return json.dumps(event, ensure_ascii=False).encode("utf-8")

    seconds = storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT) if timeout is None else timeout
    return storage.append_journal(root, build, seconds)


_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "time",
        "actor",
        "kind",
        "step",
        "name",
        "message",
        "idempotency_hash",
        "status",
        "duration_s",
        "gate",
        "mode",
        "role",
        "model",
        "level",
        "effort",
        "source",
        "path",
    }
)


def _tail_verdict(journal: bytes) -> bool | None:
    """Хвост журнала без моделей: последнее событие хода — остановка (True), другое (False) или неясно (None).

    Идём с конца и пропускаем то, что хода не двигает: нечитаемые строки, «изменено вне агента» и события
    хуков, кроме «задания исследователю» и «ответа человека». «Прервано» может быть заметкой сводки
    (она ход не двигает) — это решает полный разбор.
    """
    for raw in reversed(journal.split(b"\n")):
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if (
            kind not in _EVENT_KINDS
            or not isinstance(e.get("time"), str)
            or not isinstance(e.get("step"), str)
        ):
            continue  # JSON, но не событие: пропускается так же, как нечитаемая строка
        if e.get("schema_version") != 1 or set(e) - _EVENT_FIELDS:
            return None  # вид знакомый, но строка не по схеме: решает полный разбор
        if kind == "изменено вне агента" or (
            e.get("actor") == ACTOR and kind not in (TASK_KIND, HUMAN_ANSWER)
        ):
            continue
        if kind == "действие" and e.get("status") == "прервано":
            return None
        return kind == "действие" and e.get("name") == STOP_NAME
    return False


def _waits_for_answer(journal: bytes) -> bool:
    """Проект ждёт ответа на «внеплановую остановку»: последнее событие хода — она (`project.pause_kind`).

    Быстрый путь: остановки в журнале нет или хвост до последнего события хода не похож на остановку —
    модели проекта не подключаются. Полный разбор нужен, только когда хвост похож на остановку; строка,
    которую не разобрать, в нём пропускается и записи не отключает.
    """
    if STOP_NAME.encode("utf-8") not in journal or _tail_verdict(journal) is False:
        return False
    from pydantic import ValidationError

    from strateg import project as proj
    from strateg.models import JournalEvent

    events = []
    for raw in journal.split(b"\n"):
        if raw.strip():
            try:
                events.append(JournalEvent.model_validate_json(raw))
            except ValidationError:
                continue
    return proj.pause_kind(events) == proj.PAUSE_STOP


def _role_done_name(journal: bytes, spec: EventSpec) -> str | None:
    """Имя нового события «роль выполнена»: `<id>`, `<id>#2`, …; None — это повтор того же завершения."""
    import datetime as dt

    base = spec.name or ""
    last: dict[str, Any] | None = None
    count = 0
    for raw in journal.split(b"\n"):
        if spec.kind.encode("utf-8") not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        name = e.get("name")
        if (
            e.get("kind") == spec.kind
            and isinstance(name, str)
            and (name == base or name.startswith(base + "#"))
        ):
            last, count = e, count + 1
    if last is None:
        return base
    try:
        ago = (now() - dt.datetime.fromisoformat(last["time"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        ago = ROLE_DONE_REPEAT_SECONDS  # время не прочитать: считаем новым завершением
    if 0 <= ago < ROLE_DONE_REPEAT_SECONDS:
        return None
    return f"{base}#{count + 1}"


def _noted(journal: bytes, spec: EventSpec) -> bool:
    """В журнале уже есть событие того же вида с тем же именем (номер сессии)."""
    kind = spec.kind.encode("utf-8")
    for raw in journal.split(b"\n"):
        if kind not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if e.get("kind") == spec.kind and e.get("name") == spec.name:
            return True
    return False


def record_events(events: tuple[EventSpec, ...], ctx: Ctx) -> None:
    """Пишет события в журнал активного проекта; нет проекта — ничего.

    «Граница прав не активна» пишется раз за сессию (имя события — номер сессии).
    """
    if not events:
        return
    root = active_project_root(ctx)
    if root is None:
        return
    for spec in events:
        if spec.kind == "задание исследователю":
            spec = EventSpec(spec.kind, redact(spec.message), spec.name, spec.role)
        append_event(root, spec)
