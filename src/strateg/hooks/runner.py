"""Точки входа хуков для обёрток `.claude/hooks/*.py`: вход — байты со stdin, выход — `HookResult`.

Ответ хука — один объект JSON в стандартном выводе и больше ничего: всё, что печатают вызываемые команды
CLI, перехватывается. Сбой хука прав — отказ с текстом «хук прав не сработал: …». Сбой остальных хуков
вызов не блокирует; бюджет после прав тоже: сбой учёта — вызов пропускается и пишется «учёт недоступен».
"""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from strateg import storage
from strateg.hooks import cost, effects, flags, rules

NO_PROJECT_TEXT = "Проекта нет, начни с `strateg new`."
MAX_REASON_CHARS = 300
PEEK_SOURCES = frozenset({"compact", "fork"})
# Сроки ожидания папки: каждый хук ждёт меньше своего тайм-аута из `.claude/settings.json` (15 с).
SESSION_LOCK_TIMEOUT = 10.0  # хуки через CLI: SessionStart (сводка) и PostToolUseFailure; CLI ждёт 30 с
STOP_LOCK_TIMEOUT = 9.0  # общий срок на все записи хуков Stop и SubagentStop


@dataclass(frozen=True)
class HookResult:
    stdout: str = ""
    exit_code: int = 0
    stderr: str = ""  # пояснение для человека; не часть ответа хука


def parse_event(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("вход хука — не объект JSON")
    return data


def decision_output(decision: str, reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        ensure_ascii=False,
    )


def deny_output(reason: str) -> str:
    return decision_output("deny", reason)


def crash_output(exc: BaseException) -> str:
    """Отказ при сбое хука прав."""
    return deny_output(
        f"хук прав не сработал: {effects.clip(f'{type(exc).__name__}: {exc}', MAX_REASON_CHARS)}"
    )


# --- PreToolUse --------------------------------------------------------------------------------------------


def run_pre_tool_use(
    raw: bytes,
    env: Mapping[str, str],
    *,
    ctx: rules.Ctx | None = None,
    services: rules.Services | None = None,
) -> HookResult:
    """Права, модель роли, затем бюджет (ADR-0009, ADR-0010). `ctx` и `services` подставляют тесты."""
    if flags.hooks_off(env):
        return HookResult()
    try:
        event = parse_event(raw)
        ctx = ctx or effects.make_ctx(event, env)
        if services is None:
            services = effects.LiveServices(ctx).as_services()
        decision = rules.decide_pre_tool_use(event, ctx, services)
        note = ""
        try:
            effects.record_events(decision.events, ctx)
        except Exception as exc:
            if not decision.denied:
                raise  # без записи в журнал роль не запускаем: «всё пишется в журнал»
            note = "хук: событие «отказ хука» не записано в журнал: " + effects.clip(
                str(exc), MAX_REASON_CHARS
            )
        if decision.denied:
            return HookResult(decision.output(), stderr=note)
    except Exception as exc:
        return HookResult(crash_output(exc))
    return HookResult(budget_output(event, ctx, decision), stderr=note)


def budget_output(event: Mapping[str, Any], ctx: rules.Ctx, decision: rules.Decision) -> str:
    """Ответ после бюджета: отказ с текстом либо прежнее решение о правах. Сбой бюджет не блокирует."""
    try:
        if not rules.is_claude_code(event):
            return decision.output()  # транскрипт чужой среды не разбираем: бюджет держат скилл и CLI
        verdict = cost.gate(event, ctx)
        if not verdict.reason:
            return decision.output()
        return decision_output("ask" if verdict.ask else "deny", verdict.reason)
    except Exception:  # noqa: BLE001 - бюджет необязателен для работы
        return decision.output()


# --- SessionStart ------------------------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    from strateg.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue().strip()


def active_project() -> bool:
    from strateg import project as proj

    try:
        proj.resolve_project()
    except proj.CliError:
        return False
    return True


@contextlib.contextmanager
def _session_lock_wait() -> Iterator[None]:
    """Команда хука (сводка, запись ошибки) ждёт папку не дольше `SESSION_LOCK_TIMEOUT` на всё: открытие
    проекта и запись вместе, а не по 10 с каждое. Срок из среды, если он меньше, главнее."""
    with storage.deadline(SESSION_LOCK_TIMEOUT):
        yield


def session_summary(source: str) -> str:
    """Сводка «где остановились». После сжатия контекста и в ответвлённой сессии — `--peek`.

    При `compact` и `fork` идущий шаг прерванным не становится: сессия продолжает работу, а не начинается.
    """
    if not active_project():
        return NO_PROJECT_TEXT
    peek = source in PEEK_SOURCES
    argv = ["status", "--brief", *(["--peek"] if peek else [])]
    with _session_lock_wait():
        code, out, err = _run_cli(argv)
    title = "Сводка «где остановились» (`strateg status --brief" + (" --peek`):" if peek else "`):")
    if code != 0:
        return f"{title}\nне получена: {err or out}. Выполни `uv run strateg status`."
    return f"{title}\n{out}"


def run_session_start(raw: bytes, env: Mapping[str, str]) -> HookResult:
    if flags.hooks_off(env):
        return HookResult()
    try:
        event = parse_event(raw)
        source = event.get("source")
        text = session_summary(source if isinstance(source, str) else "startup")
        body = {"hookEventName": "SessionStart", "additionalContext": text}
        return HookResult(json.dumps({"hookSpecificOutput": body}, ensure_ascii=False))
    except Exception:
        return HookResult()  # сбой сводки работу не блокирует


# --- PostToolUseFailure ------------------------------------------------------------------------------------


def error_message(event: Mapping[str, Any]) -> str:
    """Текст для журнала: роль, обрезанный текст ошибки, строки-похожие-на-ключи вырезаны."""
    error = event.get("error")
    text = effects.clip(effects.redact(error if isinstance(error, str) else ""), effects.MAX_ERROR_CHARS)
    role = event.get("agent_type")
    prefix = f"[{role}] " if isinstance(role, str) and role.strip() else ""
    return prefix + (text or "ошибка инструмента без текста")


def run_post_tool_use_failure(raw: bytes, env: Mapping[str, str]) -> HookResult:
    if flags.hooks_off(env):
        return HookResult()
    try:
        event = parse_event(raw)
        if event.get("is_interrupt") is True:
            return HookResult()  # человек остановил инструмент сам: это не сбой
        if not active_project():
            return HookResult()
        tool = event.get("tool_name")
        argv = ["event", "error", "--message=" + error_message(event)]
        if isinstance(tool, str) and tool.strip():
            argv.append("--tool=" + tool.strip())
        with _session_lock_wait():
            _run_cli(argv)
    except Exception:
        pass  # сбой учёта работу не блокирует
    return HookResult()


# --- UserPromptSubmit --------------------------------------------------------------------------------------

HUMAN_ANSWER_TEXT = "человек ответил на внеплановую остановку (текст сообщения не записывается)"


def run_user_prompt_submit(raw: bytes, env: Mapping[str, str]) -> HookResult:
    """Сообщение человека: пока проект ждёт ответа на «внеплановую остановку», пишет событие «ответ человека».

    Событие — ход проекта: оно кончает ожидание (ADR-0010). Текст сообщения в журнал не попадает,
    только факт и время. Нет паузы, нет проекта, повторное сообщение, сбой — ничего не пишется. Хук ничего не
    печатает: его вывод ушёл бы в контекст модели. Работу он не блокирует.
    """
    try:
        found = _stop_context(raw, env)
        if found is not None:
            _, root = found
            effects.append_event(root, rules.EventSpec(effects.HUMAN_ANSWER, HUMAN_ANSWER_TEXT))
    except Exception:  # noqa: BLE001 - учёт необязателен
        pass
    return HookResult()


# --- Stop и SubagentStop -----------------------------------------------------------------------------------


def _stop_context(raw: bytes, env: Mapping[str, str]) -> tuple[dict[str, Any], Any] | None:
    """Событие и папка активного проекта; None — хук ничего не делает (выключен, чужая среда, нет проекта)."""
    if flags.hooks_off(env):
        return None
    event = parse_event(raw)
    if not rules.is_claude_code(event):
        return None
    root = effects.active_project_root(effects.make_ctx(event, env))
    return (event, root) if root is not None else None


def run_stop(raw: bytes, env: Mapping[str, str]) -> HookResult:
    """Конец хода основного агента: стоимость новых сообщений основного транскрипта (ADR-0010)."""
    try:
        found = _stop_context(raw, env)
        if found is not None:
            event, root = found
            with storage.deadline(STOP_LOCK_TIMEOUT):  # один срок на учёт и событие о сбое учёта
                cost.refresh(event, root, main=True, sweep=True)
    except Exception:  # noqa: BLE001 - сбой учёта работу не блокирует
        pass
    return HookResult()


def run_subagent_stop(raw: bytes, env: Mapping[str, str]) -> HookResult:
    """Конец работы субагента: след роли в журнале и стоимость его транскрипта (ADR-0010)."""
    try:
        found = _stop_context(raw, env)
        if found is not None:
            event, root = found
            with storage.deadline(STOP_LOCK_TIMEOUT):  # один срок на след роли, учёт и событие о сбое учёта
                cost.role_done(event, root)
                cost.refresh(event, root, main=False, sweep=False, own_required=True)
    except Exception:  # noqa: BLE001 - сбой учёта работу не блокирует
        pass
    return HookResult()
