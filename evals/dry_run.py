"""Подменённый `claude` для `evals/run.py --dry-run`: без сети и без модели.

Подменённый вызов не отвечает по существу. Он повторяет то, что сделал бы агент, командами CLI в изолированной
копии: открывает и закрывает шаги, открывает ворота, пишет ответы человека и внеплановую остановку. Проект
берётся из готового примера (`strateg new --from`). Так проверяется весь цикл: от первого вызова до
выпуска, определение ожидания, ответы по карточке и накопительная стоимость. Что именно сказал бы
агент, здесь не проверяется.

Сценарий — список ходов. Ход — команды CLI и текст ответа вызова. Слово `{prompt}` в команде заменяется
сообщением, с которым вызвали подменённый `claude`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

SESSION_ID = "dry-run-session"
EXAMPLE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "project_valid"
PROJECT_ID = "dry-run"

Command = list[str]
Turn = tuple[list[Command], str]  # команды CLI и текст ответа вызова


def scenario(mode: str) -> list[Turn]:
    """Ходы подменённого агента до выпуска; в пилоте остановки и на воротах В2, В3 и В5."""
    pilot = mode == "пилот"
    step = lambda n: ["step", "start", n]  # noqa: E731
    done = lambda n: ["step", "done", n]  # noqa: E731
    open_gate = lambda g, q: ["gate", "open", g, "--questions", str(q)]  # noqa: E731
    answer = lambda g: ["gate", "answer", g, "--answer", "{prompt}"]  # noqa: E731
    turns: list[Turn] = [
        ([["new", PROJECT_ID, "--from", str(EXAMPLE)], step("1"), open_gate("В1", 3)], "Вопросы на В1."),
    ]
    after_b1: list[Command] = [answer("В1"), done("1"), step("2а")]
    if pilot:
        turns.append((after_b1 + [open_gate("В2", 2)], "План и бюджет: принять?"))
        turns.append(
            ([answer("В2"), done("2а"), step("2б")] + [_stop()], "Остановка: признаки закрытых данных.")
        )
    else:
        turns.append(
            (
                after_b1 + [open_gate("В2", 0), done("2а"), step("2б"), _stop()],
                "Остановка: признаки закрытых данных.",
            )
        )
    to_b3: list[Command] = [done("2б"), step("3")]
    if pilot:
        turns.append((to_b3 + [open_gate("В3", 1)], "Картина ситуации: принять?"))
        turns.append(([answer("В3"), done("3"), step("4"), open_gate("В4", 2)], "Варианты: какой выбрать?"))
    else:
        turns.append(
            (
                to_b3 + [open_gate("В3", 0), done("3"), step("4"), open_gate("В4", 2)],
                "Варианты: какой выбрать?",
            )
        )
    turns.append(([answer("В4"), done("4"), step("5")], "Нужен ли отдельный план проверки?"))
    release: list[Command] = [done("5"), step("6"), done("6")]
    if pilot:
        turns.append((release + [step("7"), open_gate("В5", 1)], "Выпускать?"))
        turns.append(([answer("В5"), done("7")], "Готово."))
    else:
        turns.append((release + [step("7"), done("7")], "Готово."))
    return turns


def _stop() -> Command:
    return ["stop", "--reason", "источник с признаками закрытых данных: общедоступен ли он?"]


class FakeClaude:
    """Вызов `claude -p` без модели; вид тот же: (prompt, session_id, opts) -> (код, вывод, ошибки)."""

    def __init__(self, cli: Any, mode: str, turns: list[Turn] | None = None) -> None:
        self.cli = cli
        self.turns = turns if turns is not None else scenario(mode)
        self.prompts: list[str] = []
        self.sessions: list[str | None] = []
        self.argv: list[list[str]] = []

    def __call__(self, prompt: str, session_id: str | None, opts: Any, argv: list[str] | None = None):
        n = len(self.prompts)
        self.prompts.append(prompt)
        self.sessions.append(session_id)
        self.argv.append(argv or [])
        if (n == 0) != (session_id is None):
            return 1, "", "подменённый claude: сессия задана не там, где нужно"
        if session_id not in (None, SESSION_ID):
            return 1, "", f"подменённый claude: неизвестная сессия {session_id}"
        if n >= len(self.turns):
            return 1, "", "подменённый claude: сценарий кончился"
        commands, text = self.turns[n]
        for command in commands:
            args = [prompt if a == "{prompt}" else a for a in command]
            code, out, err = self.cli.run(*args)
            if code != 0:
                return (
                    1,
                    "",
                    f"подменённый claude: strateg {' '.join(args[:3])}: {(err or out).strip()[:300]}",
                )
        calls = n + 1
        reply: dict[str, Any] = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": SESSION_ID,
            "num_turns": 3,
            "total_cost_usd": round(
                0.5 * calls, 4
            ),  # накопительная: у настоящего `--resume` итог тоже растёт
            "usage": {"input_tokens": 100 * calls, "output_tokens": 50 * calls},
            "modelUsage": {
                "claude-sonnet-5-5": {"costUSD": round(0.4 * calls, 4)},
                "claude-haiku-4-5": {"costUSD": round(0.1 * calls, 4)},
            },
        }
        return 0, json.dumps(reply, ensure_ascii=False), ""


Invoker = Callable[..., tuple[int, str, str]]
