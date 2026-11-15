"""UserPromptSubmit (ADR-0010): ответ человека на «внеплановую остановку» — событие в журнале.

Пока проект ждёт ответа на остановку, хук пишет одно событие «ответ человека» (только факт и время, текста
сообщения нет). Нет паузы или проекта — ничего. Сбой хука работу не блокирует; в стандартный вывод хук ничего
не печатает: иначе текст ушёл бы в контекст модели.
"""

import os
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        from strateg.hooks import runner

        runner.run_user_prompt_submit(raw, os.environ)
    except BaseException:  # noqa: BLE001 - учёт необязателен
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
