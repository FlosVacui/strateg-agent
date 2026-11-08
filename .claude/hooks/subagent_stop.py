"""SubagentStop (ADR-0010): след роли в журнале и стоимость транскрипта субагента в `cost.jsonl`.

Нет активного проекта — ничего. Сбой хука работу не блокирует; в стандартный вывод хук ничего не печатает.
"""

import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        from strateg.hooks import runner

        runner.run_subagent_stop(raw, os.environ)
    except BaseException:  # noqa: BLE001 - учёт необязателен
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
