"""PostToolUse (ADR-0009, ADR-0013): сверка домена после перехода исследователя в браузере.

Тонкая обёртка: читает JSON со стандартного ввода, решение принимает `strateg.hooks.runner`. Сбой хука
работу не блокирует: итог перехода остаётся «не подтверждён», и снимок экрана закрыт.
"""

import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        from strateg.hooks import runner

        out = runner.run_post_tool_use(raw, os.environ).stdout
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - пакет не подключился: итог не записан, снимок закрыт
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
