"""Решение `PreToolUse`: права ролей по `agent_type` (ADR-0009, ADR-0007, ADR-0011, ADR-0013).

Чистые функции: вход хука, окружение (`Ctx`) и сведения о проекте (`Services`, их подставляют тесты) дают
`Decision`. Файлы и журнал трогает только `strateg.hooks.effects`.

Основной агент (нет `agent_type`): Read, Grep, Glob, Write, Edit в рабочей папке; Bash; запуск ролей проекта.
Нельзя: веб, Playwright, `strateg fetch`, сетевые программы.

Исследователь (`researcher`): `WebSearch`; чтение только в `projects/<id>/sources/` и `knowledge/`; Bash —
одна команда `uv run strateg fetch '<адрес>'`; Playwright — `browser_navigate` и `browser_take_screenshot`.

Критик (`critic`): чтение в папке проекта и `knowledge/`; остальное закрыто.

Всем ролям закрыты: `WebFetch`; `.env`, `~/.strateg/`, `.tmp/pending/`, `evals/`; правка `AGENTS.md`,
`CLAUDE.md`, `.claude/` (охрану правил снимает `STRATEG_DEV=1`).

Ложный отказ дороже пропуска для чтения в папке проекта; для веба и для Bash исследователя — наоборот.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from strateg.hooks import flags, paths, shell

RESEARCHER, CRITIC = "researcher", "critic"
MAIN, OTHER = "main", "other"

READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "Monitor"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
BUILTIN_AGENTS = ("general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide")
MODEL_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})  # модель вызова субагента — только псевдоним
PLAYWRIGHT_ALLOWED = frozenset({"browser_navigate", "browser_take_screenshot"})

# Файлы проекта, которые пишет только CLI (ADR-0002): сверяется тестом с `project_check`.
CLI_STATE_FILES = (
    "state.yaml",
    "journal.jsonl",
    "cost.jsonl",
    "sources.yaml",
    "claims.yaml",
    "frame.yaml",
    "plan.yaml",
    "gaps.yaml",
    "options.yaml",
    "promises.yaml",
    "verification.yaml",
    "decisions.yaml",
    "completeness.yaml",
)
CLI_STATE_DIRS = ("sources", "history")
CLI_STATE_SUFFIXES = (".yaml", ".yml", ".jsonl")

CURSOR_MARKERS = ("cursor_version", "conversation_id", "generation_id", "workspace_roots")
TASK_PLAN_ID = re.compile(r"ВП[-‐-―]\s?(\d+)")
TASK_FORBIDDEN_PATH = re.compile(r"(?<![\w.-])(?:inputs|outputs)\s*[/\\]", re.IGNORECASE)
MAX_TASK_CHARS = 2000


class HookError(Exception):
    """Данные для решения недоступны (таблица маршрутов не читается): вызов отклоняется с этим текстом."""


# --- Данные, с которыми работает решение -------------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    """Событие журнала, которое запишет `effects`; пишется от имени `хук`."""

    kind: str
    message: str
    name: str | None = None
    role: str | None = None
    model: str | None = None
    level: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" или "deny"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    events: tuple[EventSpec, ...] = ()

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    def output(self) -> str:
        """Ответ хука: пустая строка — «пропустить без изменений»."""
        if self.denied:
            body = {"permissionDecision": "deny", "permissionDecisionReason": self.reason}
        elif self.updated_input is not None:
            body = {"permissionDecision": "allow", "updatedInput": self.updated_input}
            if self.reason:
                body["permissionDecisionReason"] = self.reason
        else:
            return ""
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **body}}, ensure_ascii=False)


@dataclass(frozen=True)
class Pick:
    """Событие журнала «выбор модели»."""

    role: str
    model: str
    step: str
    name: str


@dataclass(frozen=True)
class ProjectView:
    id: str
    step: str
    profile: str
    picks: tuple[Pick, ...] = ()
    plan_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Choice:
    level: str
    model: str
    effort: str


@dataclass(frozen=True)
class RoutingView:
    """Модель ролей из `config/routing.yaml` с учётом профиля проекта: роль -> уровень, модель, effort."""

    roles: Mapping[str, Choice]


@dataclass(frozen=True)
class Services:
    """Всё, что решение берёт из проекта и настроек. Функции вызываются лениво: только когда нужны."""

    project: Callable[[], ProjectView | None]
    routing: Callable[[], RoutingView]
    agents: Callable[[], frozenset[str]]  # роли, у которых есть обёртка в `.claude/agents/`
    active: Callable[[], str | None] = lambda: None  # номер активного проекта; None — активного нет


@dataclass(frozen=True)
class Ctx:
    penv: paths.PathEnv
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def dev(self) -> bool:
        return flags.dev_mode(self.env)


# --- Среда и роль ------------------------------------------------------------------------------------------


def is_claude_code(event: Mapping[str, Any]) -> bool:
    """Вход похож на Claude Code: PascalCase-имя события и `session_id`, без признаков Cursor.

    Cursor называет события с маленькой буквы (`preToolUse`) и добавляет `cursor_version`, `conversation_id`
    и `workspace_roots`; `agent_type` у него в документации нет, поэтому роли там не различить.
    """
    name = event.get("hook_event_name")
    session = event.get("session_id")
    if not isinstance(name, str) or not name or not name[0].isupper():
        return False
    if not isinstance(session, str) or not session:
        return False
    return not any(k in event for k in CURSOR_MARKERS)


def role_kind(event: Mapping[str, Any]) -> str:
    """`main` (нет `agent_type`), `researcher`, `critic` или `other` (другая роль: права основного агента)."""
    at = event.get("agent_type")
    if isinstance(at, str) and at.strip():
        return at if at in (RESEARCHER, CRITIC) else OTHER
    if event.get("agent_id"):
        return OTHER
    return MAIN


def playwright_tool(name: str) -> str | None:
    """Короткое имя инструмента Playwright из `mcp__<сервер>__<инструмент>`; None — это не Playwright."""
    if not name.startswith("mcp__"):
        return None
    server, _, tool = name[len("mcp__") :].partition("__")
    if "playwright" in server.lower() or tool.startswith("browser_"):
        return tool or server
    return None


def _deny(reason: str, tool: str, event: Mapping[str, Any], extra: tuple[EventSpec, ...] = ()) -> Decision:
    at = event.get("agent_type")
    role = at if isinstance(at, str) and at.strip() else None
    return Decision(
        "deny", reason, events=(*extra, EventSpec("отказ хука", reason, name=tool or None, role=role))
    )


# --- Файлы -------------------------------------------------------------------------------------------------


def _target(tool: str, tin: Mapping[str, Any], cwd: str) -> tuple[str | None, bool]:
    """Путь, который затрагивает вызов, и признак «пути нет в входе» (Grep и Glob берут рабочую папку)."""
    if tool == "NotebookEdit":
        value = tin.get("notebook_path")
    elif tool in ("Grep", "Glob"):
        value = tin.get("path")
    else:
        value = tin.get("file_path")
    path = value if isinstance(value, str) and value.strip() else None
    if tool == "Glob":
        pattern = tin.get("pattern")
        if isinstance(pattern, str) and pattern:
            return paths.glob_target(pattern, path, cwd), path is None and not pattern.startswith(("/", "~"))
    if path is None:
        return (cwd, True) if tool in ("Grep", "Glob") else (None, False)
    return path, False


SENSITIVE_WHY = {
    ".env": "в нём ключи, агенту они не нужны",
    "~/.strateg/": "там материалы с персональными данными",
    ".tmp/pending/": "там исходник с персональными данными до `strateg add --confirm`",
    "evals/": "это стенд разработки агента, а не материалы задачи",
}


def _cli_owned(rel: tuple[str, ...]) -> str | None:
    """Файл или папка проекта, которые пишет только CLI; `rel` — путь внутри папки проекта."""
    if not rel:
        return None
    head = rel[0]
    if head in CLI_STATE_DIRS:
        return head + "/"
    if len(rel) == 1 and (head in CLI_STATE_FILES or head.endswith(CLI_STATE_SUFFIXES)):
        return head
    return None


def _cli_hint(name: str) -> str:
    if name == "claims.yaml":
        return "утверждения — `uv run strateg claim add|replace|revoke`"
    if name in ("sources.yaml", "sources/"):
        return "источники — `uv run strateg fetch` и `uv run strateg add`"
    return (
        "файлы выхода — `uv run strateg write --file projects/<id>/.tmp/<имя>.yaml`, "
        "остальное — команды `strateg`"
    )


PATTERN_SEPARATORS = re.compile(r"[/\\{},]")
OUTPUTS, TMP = "outputs", ".tmp"
CRITIC_CLOSED_DIRS = {
    "inputs": "там материалы человека",
    TMP: "там рабочие файлы агента",
    "history": "там прежние версии файлов",
}
CRITIC_HINT = "Читай выводы проекта: `outputs/` и файлы выходов."
CRITIC_CLOSED_FILES = {
    "state.yaml": "это служебное состояние проекта",
    "cost.jsonl": "это учёт стоимости",
    "completeness.yaml": "это оценка полноты, её пишет CLI",
}


def _pattern_problem(tool: str, tin: Mapping[str, Any]) -> str | None:
    """Шаблон `Glob` (и фильтр `glob` у `Grep`): `evals` в любом месте и `..` в любом сегменте — отказ."""
    value = tin.get("pattern") if tool == "Glob" else tin.get("glob") if tool == "Grep" else None
    if not isinstance(value, str) or not value:
        return None
    parts = PATTERN_SEPARATORS.split(value)
    if any(p.strip().lower() == "evals" for p in parts):
        return (
            f"Доступ к evals/ закрыт всем ролям: {SENSITIVE_WHY['evals/']}. В шаблоне `{value}` есть `evals`."
        )
    if tool == "Glob" and any(p.replace("[.]", ".").startswith("..") for p in parts):
        return (
            f"В шаблоне Glob `{value}` есть `..`: шаблон не выходит из папки `path`. "
            "Укажи `path` нужной папки и шаблон без `..`."
        )
    return None


def _write_zone(pl: paths.Place, active: str | None) -> bool:
    """Запись без `STRATEG_DEV=1`: `outputs/` и `.tmp/` активного проекта (если его нет — любого), блокнот."""
    if pl.in_scratchpad:
        return True
    if not (pl.in_projects and pl.project_id and pl.project_rel):
        return False
    if active is not None and pl.project_id != active:
        return False
    return pl.project_rel[0] in (OUTPUTS, TMP)


WRITE_ZONE_HINT = (
    "документы — в `projects/<id>/outputs/`, входы команд — в `projects/<id>/.tmp/`, "
    "остальное пишет CLI (`uv run strateg …`); правит код и правила разработчик с `STRATEG_DEV=1`"
)


def _check_search(tool: str, implicit: bool, places: list[paths.Place], svc: Services) -> str | None:
    """Поиск основного агента: `path` внутри активного проекта или `knowledge/`; без `path` отказ."""
    if implicit:
        return (
            f"У {tool} нет `path`: без него поиск идёт от корня продукта и заденет лишнее. "
            "Укажи `path`: `projects/<id>/` активного проекта или `knowledge/`."
        )
    active = svc.active()
    for pl in places:
        if pl.in_knowledge or (pl.project_id is not None and (active is None or pl.project_id == active)):
            continue
        return (
            f"Поиск {tool} — только в папке активного проекта (`projects/<id>/`) или в `knowledge/`: "
            "укажи `path` внутри них."
        )
    return None


def _check_researcher(tool: str, implicit: bool, places: list[paths.Place], svc: Services) -> str | None:
    if implicit:
        return (
            f"У {tool} нет `path`, а рабочая папка исследователю закрыта: "
            "укажи `projects/<id>/sources/` или `knowledge/`."
        )
    active = svc.active()
    for pl in places:
        if pl.in_knowledge:
            continue
        if not _is_sources(pl):
            return (
                "Исследователь читает только `sources/` активного проекта и `knowledge/`. "
                "Материалов и выводов проекта у него нет."
            )
        if active is not None and pl.project_id != active:
            return (
                f"Исследователь читает `sources/` только активного проекта «{active}», "
                f"а не «{pl.project_id}»."
            )
    return None


def _flat_listing(tool: str, tin: Mapping[str, Any]) -> bool:
    """Glob по папке проекта без захода в подпапки: `*.yaml`. Тогда закрытые папки не заденет."""
    pattern = tin.get("pattern")
    if tool != "Glob" or not isinstance(pattern, str):
        return False
    return not any(c in pattern for c in "/\\") and "**" not in pattern


def _check_critic(
    tool: str, tin: Mapping[str, Any], implicit: bool, places: list[paths.Place], svc: Services
) -> str | None:
    active = svc.active()
    for pl in places:
        if pl.in_knowledge:
            continue
        if not pl.project_id:
            hint = " Укажи `path` внутри них." if implicit else ""
            return f"Критик читает только папку проекта `projects/<id>/` и `knowledge/`.{hint}"
        if active is not None and pl.project_id != active:
            return f"Критик читает только папку активного проекта «{active}», а не «{pl.project_id}»."
        rel = pl.project_rel or ()
        if not rel:
            if tool == "Grep" or (tool == "Glob" and not _flat_listing(tool, tin)):
                return (
                    f"Критик ищет не по всей папке проекта: `inputs/`, `.tmp/` и `history/` ему закрыты. "
                    f"Укажи `path`: `outputs/`, `sources/` или файл вывода ({tool})."
                )
            continue
        head = rel[0]
        if head in CRITIC_CLOSED_DIRS:
            return f"Критик не открывает `{head}/`: {CRITIC_CLOSED_DIRS[head]}. {CRITIC_HINT}"
        if len(rel) == 1 and head in CRITIC_CLOSED_FILES:
            return f"Критик не открывает `{head}`: {CRITIC_CLOSED_FILES[head]}. {CRITIC_HINT}"
    return None


def _check_file_tool(
    tool: str,
    kind: str,
    raw: str | None,
    implicit: bool,
    cwd: str,
    ctx: Ctx,
    tin: Mapping[str, Any],
    svc: Services,
) -> str | None:
    """Причина отказа файловому инструменту или None. Все роли: чувствительное; по ролям: зоны."""
    writing = tool in WRITE_TOOLS
    if raw is None:
        return f"{tool}: в вызове нет пути."
    if kind in (RESEARCHER, CRITIC) and writing:
        who = "Исследователю" if kind == RESEARCHER else "Критику"
        return f"{who} запись файлов закрыта: роль ничего не записывает."
    problem = _pattern_problem(tool, tin)
    if problem:
        return problem
    places = paths.places(raw, ctx.penv, cwd=cwd)
    if places is None:
        return f"Путь «{raw}» не удалось привести к одному виду (переменная, диск без черты): отказ."
    for pl in places:
        if pl.sensitive:
            name = pl.sensitive_name()
            return (
                f"Доступ к {name} закрыт всем ролям: {SENSITIVE_WHY[name]}. "
                "Материалы проекта — через `uv run strateg add`."
            )
    if writing and not ctx.dev:
        for pl in places:
            if pl.rules:
                return (
                    "Правила агента (`AGENTS.md`, `CLAUDE.md`, `.claude/`) не правятся во время работы: "
                    "их меняет разработчик с `STRATEG_DEV=1`."
                )
    if writing:
        for pl in places:
            owned = _cli_owned(pl.project_rel) if pl.project_id and pl.project_rel is not None else None
            if owned:
                return (
                    f"Состояние проекта пишет только CLI, а не {tool}: {owned} — через `strateg`. "
                    f"Подсказка: {_cli_hint(owned)}."
                )
    if kind == RESEARCHER:
        return _check_researcher(tool, implicit, places, svc)
    if kind == CRITIC:
        return _check_critic(tool, tin, implicit, places, svc)
    # основной агент и прочие роли: рабочая папка, папка проектов, служебные папки Claude Code
    if not all(_main_may(pl, writing=writing) for pl in places):
        return (
            f"{tool} вне рабочей папки закрыт. Материалы пользователя — в `projects/<id>/inputs/`, "
            "затем `uv run strateg add '<путь>'`."
        )
    if tool in ("Grep", "Glob"):
        return _check_search(tool, implicit, places, svc)
    if writing and not ctx.dev:
        active = svc.active()
        if not all(_write_zone(pl, active) for pl in places):
            return f"{tool}: запись только в `outputs/` и `.tmp/` активного проекта — {WRITE_ZONE_HINT}."
    return None


def _main_may(pl: paths.Place, *, writing: bool) -> bool:
    """Основной агент: рабочая папка, папка проектов, блокнот сеанса; читать можно и служебное Claude Code."""
    return pl.in_root or pl.in_projects or pl.in_scratchpad or (pl.in_claude_dir and not writing)


def _is_sources(pl: paths.Place) -> bool:
    return pl.project_id is not None and pl.project_rel is not None and pl.project_rel[:1] == ("sources",)


# --- Bash, поиск, браузер ----------------------------------------------------------------------------------


def _decide_shell(
    tool: str, kind: str, tin: Mapping[str, Any], cwd: str, ctx: Ctx, svc: Services
) -> str | None:
    command = tin.get("command")
    if kind == CRITIC:
        return "Критику закрыто всё, кроме чтения (Read, Grep, Glob) в папке проекта и `knowledge/`."
    if kind == RESEARCHER:
        if tool != "Bash":
            return f"Исследователю закрыт {tool}: страницы — `uv run strateg fetch '<адрес>'` в Bash."
        check = (
            shell.fetch_command(command)
            if isinstance(command, str)
            else shell.FetchCheck(False, "Команды нет")
        )
        if check.ok:
            return None
        return (
            "Исследователю разрешена одна команда Bash: `uv run strateg fetch '<адрес>'` "
            "(можно `--region <код>`), без `;`, `|`, `&`, `$(`, обратных кавычек и перенаправлений. "
            f"{check.why}."
        )
    if tool == "Monitor" and tin.get("ws"):
        return (
            "Сеть закрыта основному агенту (WebSocket в Monitor): веб — через исследователя (`researcher`)."
        )
    if not isinstance(command, str) or not command.strip():
        return f"{tool}: в вызове нет команды."
    found = shell.network_reason(command)
    if found:
        return (
            f"Сеть закрыта основному агенту ({found}): веб — через исследователя (субагент `researcher`), "
            "страницы он читает командой `strateg fetch`."
        )
    mention = shell.sensitive_mention(command)
    if mention:
        return f"Доступ к {mention} закрыт всем ролям: {SENSITIVE_WHY[mention]}."
    if not ctx.dev:
        return _bash_write_reason(command, cwd, ctx, svc)
    return None


def _bash_write_reason(command: str, cwd: str, ctx: Ctx, svc: Services) -> str | None:
    """Запись через Bash (перенаправление, `tee`, `sed -i`, `cp`, `rm`, `Set-Content` …) вне зоны записи."""
    targets = shell.write_targets(command)
    if not targets:
        return None
    active = svc.active()
    for raw in targets:
        found = paths.places(raw, ctx.penv, cwd=cwd)
        if found is None:
            continue  # переменная или другая запись, которую нельзя привести: Bash разбирается лишь отчасти
        if not all(_write_zone(pl, active) for pl in found):
            return (
                f"Запись в «{raw}» через Bash закрыта: только `outputs/` и `.tmp/` активного проекта — "
                f"{WRITE_ZONE_HINT}."
            )
    return None


def _decide_playwright(tool: str, kind: str, tin: Mapping[str, Any]) -> str | None:
    short = playwright_tool(tool) or ""
    if kind != RESEARCHER:
        return (
            "Браузер закрыт: снимки экрана интерфейсов делает исследователь, основной агент и критик — нет."
        )
    if short not in PLAYWRIGHT_ALLOWED:
        return (
            "Исследователю из браузера разрешены только `browser_navigate` и `browser_take_screenshot`; "
            f"{short or tool} закрыт."
        )
    if short == "browser_take_screenshot" and tin.get("filename") is not None:
        return "У `browser_take_screenshot` параметр `filename` закрыт: имя файла задаёт сам инструмент."
    if short == "browser_navigate":
        url = tin.get("url")
        scheme = url.split(":", 1)[0].lower() if isinstance(url, str) and ":" in url else ""
        if scheme not in ("http", "https"):
            return "`browser_navigate` ходит только по адресам http и https."
    return None


# --- Запуск субагента --------------------------------------------------------------------------------------


def _unknown_role(name: str, routing: RoutingView | None, wrappers: frozenset[str]) -> str:
    keys = frozenset(routing.roles) if routing else wrappers
    keyed = sorted(keys)
    options = ", ".join(f"`{r}`" for r in sorted(keys & wrappers)) or "нет"
    if name in BUILTIN_AGENTS:
        return (
            f"Встроенный субагент «{name}» закрыт: через него вернулись бы веб и Bash без границы прав. "
            f"Зови роль проекта: {options}."
        )
    if name in keys and name not in wrappers:
        return (
            f"У роли «{name}» нет обёртки в `.claude/agents/`: исполни роль сам по запасному пути "
            "(правило «Роли» в `AGENTS.md`)."
        )
    if name in wrappers and name not in keys:
        return f"Роли «{name}» нет в `config/routing.yaml` (есть: {', '.join(keyed)}): модель взять неоткуда."
    return f"Неизвестная роль «{name}»: допустимы {options}."


def check_task(prompt: str, plan_ids: frozenset[str]) -> str | None:
    """Задание исследователю: номера `ВП-n` есть в плане, путей к `inputs/` и `outputs/` нет."""
    unknown = sorted({f"ВП-{n}" for n in TASK_PLAN_ID.findall(prompt)} - plan_ids)
    if unknown:
        return (
            f"В задании исследователю назван вопрос {', '.join(unknown)}, которого нет в `plan.yaml`. "
            "Укажи номер из плана или пиши формулировку без номера."
        )
    if TASK_FORBIDDEN_PATH.search(prompt):
        return (
            "В задании исследователю есть путь к `inputs/` или `outputs/`: данных проекта у него нет. "
            "Передай только вопрос: формулировка, предмет, период, регион."
        )
    return None


def _model_for(role: str, view: ProjectView | None, routing: RoutingView) -> tuple[str, EventSpec | None]:
    choice = routing.roles[role]
    if view is not None:
        for pick in reversed(view.picks):
            if pick.role == role and pick.step == view.step:
                return pick.model, None  # `strateg route` уже вызван на этом шаге; повышение — тоже выбор
        event = EventSpec(
            "выбор модели",
            f"route не вызван: модель роли взята хуком из config/routing.yaml; профиль {view.profile}",
            name="выбор",
            role=role,
            model=choice.model,
            level=choice.level,
            effort=choice.effort,
        )
        return choice.model, event
    return choice.model, None


def _decide_agent(kind: str, tin: Mapping[str, Any], event: Mapping[str, Any], svc: Services) -> Decision:
    tool = str(event.get("tool_name"))
    if kind in (RESEARCHER, CRITIC):
        return _deny("Роли не запускают субагентов: это делает основной агент.", tool, event)
    raw_name = tin.get("subagent_type")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "general-purpose"
    try:
        wrappers = svc.agents()
        if (
            name in BUILTIN_AGENTS
        ):  # таблицу маршрутов для отказа читать незачем: быстрее и без тяжёлых модулей
            return _deny(_unknown_role(name, None, wrappers), tool, event)
        routing = svc.routing()
        if name not in routing.roles or name not in wrappers:
            return _deny(_unknown_role(name, routing, wrappers), tool, event)
        view = svc.project()
    except HookError as exc:
        return _deny(str(exc), tool, event)
    events: list[EventSpec] = []
    if name == RESEARCHER:
        prompt = tin.get("prompt")
        text = prompt.strip() if isinstance(prompt, str) else ""
        if not text:
            return _deny("Задание исследователю пустое: нужен вопрос плана или адрес страницы.", tool, event)
        problem = check_task(text, view.plan_ids if view else frozenset())
        if problem:
            return _deny(problem, tool, event)
    model, pick_event = _model_for(name, view, routing)
    if model not in MODEL_ALIASES:
        return _deny(
            f"Модель «{model}» из `config/routing.yaml` не псевдоним Claude Code "
            f"({', '.join(sorted(MODEL_ALIASES))}): вызов субагента принимает только псевдоним.",
            tool,
            event,
        )
    if pick_event is not None:
        events.append(pick_event)
    if name == RESEARCHER:
        events.append(EventSpec("задание исследователю", text[:MAX_TASK_CHARS], name=None, role=name))
    updated = {**tin, "subagent_type": name, "model": model}
    return Decision("allow", f"модель роли {name}: {model}", updated_input=updated, events=tuple(events))


# --- Среда без ролей ---------------------------------------------------------------------------------------


def _decide_foreign(event: Mapping[str, Any], ctx: Ctx) -> Decision:
    """Вход не из Claude Code: ролей не различить, права по ролям не применяются (ADR-0009).

    Действует охрана `.env`, `~/.strateg/`, `.tmp/pending/` и правил; пишется событие
    «граница прав не активна».
    """
    session = event.get("session_id")
    note = EventSpec(
        "граница прав не активна",
        "вход хука не похож на Claude Code (нет hook_event_name или session_id либо есть признаки Cursor): "
        "права по ролям не применяются; охрана правил, .env, ~/.strateg/ и .tmp/pending/ действует",
        name=session if isinstance(session, str) and session else "без номера сессии",
    )
    tool = str(event.get("tool_name") or "")
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else ctx.penv.cwd
    tool = {"Shell": "Bash"}.get(tool, tool)
    reason = None
    if tool in READ_TOOLS | WRITE_TOOLS:
        raw, _ = _target(tool, tin, cwd)
        reason = _check_guard_only(tool, raw, cwd, ctx)
    elif tool in SHELL_TOOLS:
        command = tin.get("command")
        mention = shell.sensitive_mention(command, evals=False) if isinstance(command, str) else None
        if mention:
            reason = f"Доступ к {mention} закрыт: {SENSITIVE_WHY[mention]}."
    if reason:
        return _deny(reason, tool, event, extra=(note,))
    return Decision("allow", events=(note,))


def _check_guard_only(tool: str, raw: str | None, cwd: str, ctx: Ctx) -> str | None:
    if raw is None:
        return None
    places = paths.places(raw, ctx.penv, cwd=cwd)
    if places is None:
        return f"Путь «{raw}» не удалось привести к одному виду: отказ."
    for pl in places:
        if pl.env_file or pl.strateg_home or pl.pending:  # `evals/` без ролей не закрываем
            name = pl.sensitive_name()
            return f"Доступ к {name} закрыт: {SENSITIVE_WHY[name]}."
    if tool in WRITE_TOOLS and not ctx.dev and any(pl.rules for pl in places):
        return "Правила агента (`AGENTS.md`, `CLAUDE.md`, `.claude/`) не правятся во время работы."
    return None


# --- Точка входа -------------------------------------------------------------------------------------------


def decide_pre_tool_use(event: Mapping[str, Any], ctx: Ctx, services: Services) -> Decision:
    """Решение по входу `PreToolUse`. Любое исключение внутри обрабатывает `runner`: оно даёт отказ."""
    if not is_claude_code(event):
        return _decide_foreign(event, ctx)
    tool = str(event.get("tool_name") or "")
    raw_input = event.get("tool_input")
    tin: Mapping[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    kind = role_kind(event)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else ctx.penv.cwd

    if tool == "WebFetch":
        return _deny(
            "WebFetch закрыт всем ролям: он отдаёт пересказ мимо снимка. "
            "Страницы читает исследователь командой `uv run strateg fetch`.",
            tool,
            event,
        )
    if tool in READ_TOOLS | WRITE_TOOLS:
        raw, implicit = _target(tool, tin, cwd)
        reason = _check_file_tool(tool, kind, raw, implicit, cwd, ctx, tin, services)
        return _deny(reason, tool, event) if reason else Decision("allow")
    if tool in SHELL_TOOLS:
        reason = _decide_shell(tool, kind, tin, cwd, ctx, services)
        return _deny(reason, tool, event) if reason else Decision("allow")
    if tool in AGENT_TOOLS:
        return _decide_agent(kind, tin, event, services)
    if tool == "WebSearch":
        if kind == RESEARCHER:
            return Decision("allow")
        return _deny("Веб-поиск закрыт: веб — через исследователя (субагент `researcher`).", tool, event)
    if playwright_tool(tool) is not None:
        reason = _decide_playwright(tool, kind, tin)
        return _deny(reason, tool, event) if reason else Decision("allow")
    if kind in (RESEARCHER, CRITIC):
        who = "Исследователю" if kind == RESEARCHER else "Критику"
        return _deny(f"{who} инструмент {tool or '(без имени)'} не разрешён.", tool, event)
    return Decision("allow")
