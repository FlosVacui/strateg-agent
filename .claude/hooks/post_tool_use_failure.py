"""PostToolUseFailure (ADR-0009): ошибка инструмента — в журнал проекта (`strateg event error`).

Нет активного проекта — ничего. Сбой хука работу не блокирует.
"""

import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        from strateg.hooks import runner

        runner.run_post_tool_use_failure(raw, os.environ)
    except BaseException:  # noqa: BLE001 - учёт необязателен
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
