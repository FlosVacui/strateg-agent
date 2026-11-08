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


def append_event(root: Path, spec: EventSpec) -> bool:
    """Добавляет событие в `journal.jsonl` проекта и обновляет в `state.yaml` только хеш журнала.

    Запись идёт тем же кодом, что и у CLI (`strateg.storage`): под блокировкой папки проекта, поэтому
    параллельные хуки и команда CLI не теряют событий и не дают ложного «изменено вне агента». Хеши других
    файлов не трогаются: правку человека принимает только команда CLI. Модели проекта не подключаются: хук
    на отказе должен отвечать быстро; правильность строки проверяют тесты (`check_project`). Возвращает
    False, если проект не удалось прочитать. Папка занята дольше таймаута — `storage.LockTimeout`.
    """
    import yaml

    def build(state_bytes: bytes, journal: bytes) -> bytes | None:
        if spec.kind == ONCE_PER_SESSION and _noted(journal, spec):
            return None  # проверка под блокировкой: параллельные хуки одной сессии пишут событие один раз
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
            value = getattr(spec, key)
            if value is not None:
                event[key] = value
        return json.dumps(event, ensure_ascii=False).encode("utf-8")

    return storage.append_journal(root, build, storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT))


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
