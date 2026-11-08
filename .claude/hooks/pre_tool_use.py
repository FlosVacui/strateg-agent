"""PreToolUse (ADR-0009): права ролей, запуск субагента, модель роли.

Тонкая обёртка: читает JSON со стандартного ввода, решение принимает `strateg.hooks.runner`.
В стандартный вывод идёт только ответ JSON. Сбой хука прав блокирует вызов.
"""

import json
import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    if os.environ.get("STRATEG_HOOKS", "").strip() == "0":  # выключено человеком, только для замера
        return 0
    try:
        from strateg.hooks import runner

        result = runner.run_pre_tool_use(raw, os.environ)
        out = result.stdout
        if result.stderr:  # потерянное событие журнала не молчит
            sys.stderr.buffer.write(result.stderr.encode("utf-8") + b"\n")
            sys.stderr.buffer.flush()
    except BaseException as exc:  # noqa: BLE001 - пакет не подключился или сбой до решения: отказ
        reason = f"хук прав не сработал: {type(exc).__name__}: {exc}"[:300]
        out = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
