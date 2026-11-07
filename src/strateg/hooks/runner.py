"""Точки входа хуков для обёрток `.claude/hooks/*.py`: вход — байты со stdin, выход — `HookResult`.

Ответ хука — один объект JSON в стандартном выводе и больше ничего: всё, что печатают вызываемые команды
CLI, перехватывается. Сбой хука прав — отказ с текстом «хук прав не сработал: …». Сбой остальных хуков
вызов не блокирует.
"""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from strateg.hooks import effects, flags, rules

NO_PROJECT_TEXT = "Проекта нет, начни с `strateg new`."
MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class HookResult:
    stdout: str = ""
    exit_code: int = 0


def parse_event(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("вход хука — не объект JSON")
    return data


def deny_output(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        ensure_ascii=False,
    )


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
    """Права и модель роли. `ctx` и `services` подставляют тесты; на деле их собирает `effects`."""
    if flags.hooks_off(env):
        return HookResult()
    try:
        event = parse_event(raw)
        ctx = ctx or effects.make_ctx(event, env)
        if services is None:
            services = effects.LiveServices(ctx).as_services()
        decision = rules.decide_pre_tool_use(event, ctx, services)
        try:
            effects.record_events(decision.events, ctx)
        except Exception:
            if not decision.denied:
                raise  # без записи в журнал роль не запускаем: «всё пишется в журнал»
        return HookResult(decision.output())
    except Exception as exc:
        return HookResult(crash_output(exc))


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


def session_summary(source: str) -> str:
    """Сводка «где остановились». После сжатия контекста — `--peek`: идущий шаг прерванным не становится."""
    if not active_project():
        return NO_PROJECT_TEXT
    peek = source == "compact"
    argv = ["status", "--brief", *(["--peek"] if peek else [])]
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
        _run_cli(argv)
    except Exception:
        pass  # сбой учёта работу не блокирует
    return HookResult()
