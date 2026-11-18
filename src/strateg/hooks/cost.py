"""Учёт стоимости и бюджет в хуках (ADR-0010, ADR-0009).

- `refresh` — дочитывает транскрипты и дописывает стоимость в `cost.jsonl` (`strateg.accounting`). Сбой не
  бросает исключение: пишется событие «учёт недоступен» (одно на сессию, только вид ошибки), работа не
  блокируется.
- `gate` — проверка бюджета в `PreToolUse` после решения о правах. Перед платным вызовом (запуск субагента,
  `WebSearch`, `uv run strateg fetch`, инструменты Playwright), перед командами `strateg step …` (расход
  уходит на старый шаг) и `strateg budget set …` расход пересчитывается по транскриптам всегда; перед
  остальными вызовами, которые исчерпание закрыло бы, — не чаще раза в `RECOUNT_EVERY` секунд. Если деньги
  или время исчерпаны, отказ получают все вызовы, кроме команд `uv run strateg …` (не `fetch`), чтения в
  папке проекта и записи в `outputs/` — для выпуска с пометкой о неполноте. Среди открытых команд
  `budget set` с повышением лимита получает ответ `ask`: решает человек, а не агент. Вся бюджетная часть
  укладывается в `GATE_SECONDS`: хук имеет единственный срок на все блокировки, остаток передаётся дальше.
  Если учёт недоступен или срок вышел, вызов пропускается: бюджет держит время и CLI.

Тяжёлые модули (`budget`, `accounting`, `project`) подключаются внутри функций: хук прав на каждый вызов
инструмента должен стартовать быстро, а бюджет нужен только платному вызову и вызову, который не относится
к разрешённым при исчерпании.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strateg import storage
from strateg.hooks import effects, paths, rules, shell

MAX_REASON_CHARS = 300
GATE_SECONDS = 2.0  # срок всей бюджетной части PreToolUse: пересчёт, чтение проекта; таймаут хука — 15 с
NOTE_SECONDS = 0.5  # запись события «учёт недоступен» внутри этого срока
RECOUNT_EVERY = 5.0  # не чаще раза в столько секунд — пересчёт перед вызовами, которые не платные
NO_SESSION = "без номера сессии"
BUDGET_KIND = "бюджет"
ROLE_DONE = "роль выполнена"
ACCOUNTING_DOWN = "учёт недоступен"
FILE_TOOLS_READ = rules.READ_TOOLS
FILE_TOOLS_WRITE = rules.WRITE_TOOLS


def _session(event: Mapping[str, Any]) -> str:
    value = event.get("session_id")
    return value if isinstance(value, str) and value.strip() else NO_SESSION


def _role(event: Mapping[str, Any]) -> str | None:
    value = event.get("agent_type")
    return value if isinstance(value, str) and value.strip() else None


def _note(root: Path, spec: rules.EventSpec, timeout: float | None = None) -> None:
    """Запись события журнала; сбой записи работу не блокирует."""
    try:
        effects.append_event(root, spec, timeout)
    except Exception:  # noqa: BLE001 - событие необязательно: учёт не должен ломать ход
        pass


def accounting_down(
    root: Path, event: Mapping[str, Any], exc: BaseException, timeout: float | None = None
) -> None:
    """«Учёт недоступен» — один раз за сессию. В журнал идёт только вид ошибки: текст исключения у `OSError`
    содержит полный путь файла (имя пользователя), а журнал уходит в архив проекта."""
    _note(
        root,
        rules.EventSpec(
            ACCOUNTING_DOWN,
            f"учёт стоимости по транскриптам не удался: {type(exc).__name__}. Бюджет держат время и CLI",
            name=_session(event),
        ),
        timeout,
    )


def refresh(
    event: Mapping[str, Any],
    root: Path,
    *,
    main: bool,
    sweep: bool,
    own_required: bool = False,
    timeout: float | None = None,
    note_timeout: float | None = None,
) -> Any | None:
    """Дописывает стоимость новых сообщений. Результат разбора (`accounting.Result`); None — сбой (событие
    записано, если успело). `timeout` — сколько ждать блокировку проекта, `note_timeout` — сколько ждать при
    записи события."""
    try:
        from strateg import accounting

        sources = accounting.sources_for(event, main=main, sweep=sweep, own_required=own_required)
        if not sources:
            raise ValueError("в событии нет пути к транскрипту")
        result = accounting.account(root, _session(event), sources, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - любой сбой разбора: «учёт недоступен», не отказ
        accounting_down(root, event, exc, note_timeout)
        return None
    for model in result.unpriced:
        _note(
            root,
            rules.EventSpec(
                ACCOUNTING_DOWN,
                f"модели {model} нет в config/prices.yaml: токены записаны, сумма «нет цены»",
                name=f"нет цены: {model}",
            ),
            note_timeout,
        )
    return result


def role_done(event: Mapping[str, Any], root: Path, timeout: float | None = None) -> None:
    """След роли: `SubagentStop` пишет «роль выполнена» с `agent_type`; пустой тип (служебный агент) — нет.

    Имя события — `agent_id`; возобновлённый субагент завершается снова и оставляет новое событие
    (`strateg.hooks.effects`), повтор того же хука — нет.
    """
    role = _role(event)
    if role is None:
        return
    agent_id = event.get("agent_id")
    name = agent_id if isinstance(agent_id, str) and agent_id.strip() else f"без id {uuid.uuid4().hex[:8]}"
    _note(
        root,
        rules.EventSpec(
            ROLE_DONE,
            f"роль {role} завершила работу",
            name=name,
            role=role,
        ),
        timeout,
    )


# --- Бюджет в PreToolUse -----------------------------------------------------------------------------------


def is_paid(tool: str, tin: Mapping[str, Any]) -> bool:
    """Платный вызов (ADR-0010): запуск субагента, веб-поиск, Playwright и `uv run strateg fetch`."""
    if tool in rules.AGENT_TOOLS or tool == "WebSearch" or rules.playwright_tool(tool) is not None:
        return True
    if tool in rules.SHELL_TOOLS:
        command = tin.get("command")
        return isinstance(command, str) and shell.strateg_calls(command).fetch
    return False


def _project_places(raw: str, ctx: rules.Ctx, cwd: str, active: str) -> list[paths.Place] | None:
    found = paths.places(raw, ctx.penv, cwd=cwd)
    if found is None:
        return None
    ok = all(pl.in_projects and pl.project_id == active and pl.project_rel is not None for pl in found)
    return found if ok else None


def allowed_when_exhausted(
    tool: str, tin: Mapping[str, Any], ctx: rules.Ctx, cwd: str, active: str | None
) -> bool:
    """Что остаётся доступным при исчерпании бюджета (ADR-0010): команды `strateg`, чтение в папке проекта,
    запись в `outputs/`. `fetch` платный и сюда не входит."""
    if tool in rules.SHELL_TOOLS:
        command = tin.get("command")
        if not isinstance(command, str):
            return False
        calls = shell.strateg_calls(command)
        return calls.only and not calls.fetch
    if active is None or tool not in FILE_TOOLS_READ | FILE_TOOLS_WRITE:
        return False
    raw, implicit = rules._target(tool, tin, cwd)  # noqa: SLF001 - тот же разбор пути, что у прав
    if raw is None or implicit:
        return False
    found = _project_places(raw, ctx, cwd, active)
    if found is None:
        return False
    if tool in FILE_TOOLS_WRITE:
        return all(pl.project_rel and pl.project_rel[0] == rules.OUTPUTS for pl in found)
    return True


OPTIONS = (
    "Решает человек, спроси его: "
    "(1) добавить бюджет — `uv run strateg budget set --usd <число> --minutes <число>`; "
    "(2) выпустить то, что есть, с пометкой о неполноте — `uv run strateg budget check --action выпуск`, "
    "затем документы в `outputs/`; "
    "(3) остановиться — `uv run strateg status`."
)


def denial_text(tool: str, reasons: list[str], summary: str) -> str:
    what = " и ".join(reasons)
    return (
        f"Бюджет исчерпан, кончилось: {what} ({summary}). Вызов {tool or 'инструмента'} закрыт: "
        "остаются команды `uv run strateg …` (кроме `fetch`), чтение в папке проекта и запись в `outputs/`. "
        + OPTIONS
    )


@dataclass(frozen=True)
class Gate:
    """Решение по бюджету: `reason` пусто — вызов пропускается; `ask` — не отказ, а вопрос человеку."""

    reason: str = ""
    ask: bool = False


def _limit_flag(flag: str) -> str | None:
    """Какой лимит называет флаг `budget set`: «usd» или «minutes»; сокращения (`--us`) тоже работают."""
    for key in ("usd", "minutes"):
        if len(flag) >= 3 and f"--{key}".startswith(flag):
            return key
    return None


def raises_limit(args: tuple[str, ...], limit_usd: float | None, limit_minutes: float) -> bool:
    """Аргументы `budget set` могут повысить лимит. True, пока не доказано обратное: незнакомый флаг, число,
    которое не разобрать, и значение выше нынешнего лимита считаются повышением."""
    asked: dict[str, float] = {}
    i = 0
    while i < len(args):
        flag, eq, value = args[i].partition("=")
        key = _limit_flag(flag) if flag.startswith("--") else None
        if key is None:
            return True
        if not eq:
            i += 1
            value = args[i] if i < len(args) else ""
        try:
            asked[key] = float(value)
        except ValueError:
            return True
        i += 1
    for key, number in asked.items():
        current = limit_usd if key == "usd" else limit_minutes
        if current is not None and number > current:
            return True
    return False


def ask_text(tool: str, reasons: list[str], summary: str) -> str:
    what = " и ".join(reasons)
    return (
        f"Бюджет исчерпан, кончилось: {what} ({summary}). Вызов {tool or 'инструмента'} повышает лимит: "
        "бюджет добавляет человек, а не агент, поэтому нужно его подтверждение. Если человека нет, "
        "остальные ответы: выпустить то, что есть, с пометкой о неполноте — "
        "`uv run strateg budget check --action выпуск`, затем документы в `outputs/`; остановиться — "
        "`uv run strateg status`."
    )


def _calls(tool: str, tin: Mapping[str, Any]) -> shell.StrategCalls | None:
    command = tin.get("command") if tool in rules.SHELL_TOOLS else None
    return shell.strateg_calls(command) if isinstance(command, str) else None


def gate(event: Mapping[str, Any], ctx: rules.Ctx) -> Gate:
    """Проверка бюджета для разрешённого правами вызова. Сбой учёта и выход за срок вызов не блокируют."""
    tool = str(event.get("tool_name") or "")
    raw_input = event.get("tool_input")
    tin: Mapping[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    root = effects.active_project_root(ctx)
    if root is None:
        return Gate()
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else ctx.penv.cwd
    paid = is_paid(tool, tin)
    open_call = not paid and allowed_when_exhausted(tool, tin, ctx, cwd, root.name)
    calls = _calls(tool, tin)
    sets = calls.budget_sets if calls is not None else ()
    # Всегда свежий расход: перед платным вызовом, перед командой, меняющей шаг (расход уйдёт на старый шаг),
    # и перед `budget set` (от расхода зависит, спрашивать ли человека).
    fresh = paid or bool(sets) or (calls is not None and calls.step)
    if open_call and not fresh:
        return Gate()  # быстрый путь: этот вызов бюджет не закрывает, и платным он не бывает
    deadline = time.monotonic() + storage.lock_timeout(GATE_SECONDS)

    def left() -> float:
        return max(deadline - time.monotonic(), 0.0)

    try:
        recounted = False
        if not fresh:
            from strateg import offsets

            ago, exhausted = offsets.recount_state(
                root
            )  # лёгкий модуль: ни `pydantic`, ни проект не читаются
            recounted = ago is not None and ago < RECOUNT_EVERY
            if recounted and exhausted is False:
                return Gate()  # недавно пересчитали, бюджета хватало: этот вызов не повод читать проект снова
        if not recounted:
            done = refresh(event, root, main=True, sweep=True, timeout=left(), note_timeout=NOTE_SECONDS)
            if done is None:
                return Gate()  # учёт недоступен (событие записано): вызов пропускаем
        if open_call and not sets:
            return Gate()  # команда `strateg step …`: расход записан, больше ничего не нужно
        from strateg import budget
        from strateg import project as proj

        p = proj.Project.load(root, timeout=left())
        m = budget.measure(p)
        reasons = budget.exhausted_reasons(m)
        if not reasons:
            return Gate()
        if open_call:  # открытая команда с `budget set`: спрашиваем человека, только если лимит растёт
            if not any(raises_limit(a, m.limit_usd, m.limit_minutes) for a in sets):
                return Gate()
            head = f"бюджет исчерпан ({' и '.join(reasons)}), хук спросил человека о вызове {tool}:"
            text, ask = ask_text(tool, reasons, budget.summary_text(m)), True
        else:
            head = f"бюджет исчерпан ({' и '.join(reasons)}), хук отказал вызову {tool or 'инструмента'}:"
            text, ask = denial_text(tool, reasons, budget.summary_text(m)), False
        last = next((e for e in reversed(proj.own_events(p.journal)) if e.kind == BUDGET_KIND), None)
        if not (last is not None and (last.message or "").startswith(head)):
            _note(
                root,
                rules.EventSpec(BUDGET_KIND, f"{head} {budget.summary_text(m)}", role=_role(event)),
                NOTE_SECONDS,
            )
        return Gate(text, ask)
    except Exception as exc:  # noqa: BLE001 - проект не читается, срок вышел и т. п.: пропускаем, время держит CLI
        accounting_down(root, event, exc, NOTE_SECONDS)
        return Gate()
