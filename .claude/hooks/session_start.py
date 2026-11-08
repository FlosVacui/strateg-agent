"""SessionStart (ADR-0009): сводка «где остановились» в контекст сессии.

`startup`, `resume`, `clear` — `strateg status --brief`; `compact` — `status --brief --peek`.
Сбой хука работу не блокирует.
"""

import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        from strateg.hooks import runner

        out = runner.run_session_start(raw, os.environ).stdout
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - сводка необязательна
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
