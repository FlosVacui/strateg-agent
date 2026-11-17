"""Точка входа CLI `strateg`: `doctor` и команды проекта (`new`, `use`, `status`, `step`, `gate`, `fetch`,
`add`, `claim`, `write`, `event`, `check`, `cost`, `budget`, `pack`, `rubric`, `route`, `due`, `impact`,
`metric`, `diff`, `search`). Сами команды
проекта — в `strateg.commands`, проверка полноты — в `strateg.rubric`, маршрут роли — в `strateg.routing`.

Коды возврата: 0 — успех, 1 — ошибка данных или правил, 2 — ошибка использования.
"""

from __future__ import annotations

import argparse
import locale
import os
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

from strateg import __version__, commands, costreport, followup, routing, rubric, search
from strateg.hooks import flags as hookflags

OK, WARN, ERR = "OK", "WARN", "ERR"
_MARKS = {OK: "OK  ", WARN: "ПРЕД", ERR: "НЕТ "}

CHCP_HINT = "выполните `chcp 65001` или откройте Windows Terminal"


def _is_utf8(name: str) -> bool:
    return name.lower().replace("-", "").replace("_", "") == "utf8"


def _windows_console_cp() -> int:
    """Кодовая страница вывода консоли Windows; 0 — консоли нет (например, в CI)."""
    import ctypes

    return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]


def _stdout_encoding() -> str:
    return sys.stdout.encoding or locale.getpreferredencoding(False)


def _is_windows() -> bool:
    """Отдельная функция, чтобы тесты подменяли платформу без подмены sys.platform на весь процесс."""
    return sys.platform == "win32"


def detect_console_encoding() -> tuple[str, str]:
    """Уровень и пояснение для кодировки консоли.

    Вызывать ДО `sys.stdout.reconfigure`: после перенастройки Python всегда говорит utf-8,
    даже если консоль показывает текст в cp866 или cp1251.
    """
    if _is_windows():
        try:
            cp = _windows_console_cp()
        except (AttributeError, OSError):
            return WARN, f"кодовую страницу консоли определить не удалось; если текст искажён, {CHCP_HINT}"
        if cp == 65001:
            return OK, "UTF-8 (кодовая страница 65001)"
        if cp == 0:
            return OK, "консоли нет, вывод идёт в файл или канал"
        return WARN, f"кодовая страница консоли {cp}, кириллица может исказиться: {CHCP_HINT}"
    enc = _stdout_encoding()
    if _is_utf8(enc):
        return OK, enc
    return WARN, f"{enc}, кириллица может исказиться: задайте UTF-8 (например, LANG=ru_RU.UTF-8)"


def find_repo_root() -> Path | None:
    """Корень репозитория — ближайшая папка с pyproject.toml (от модуля, затем от текущей папки)."""
    for start in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (start, *start.parents):
            if (parent / "pyproject.toml").is_file():
                return parent
    return None


def _check_writable(folder: Path) -> tuple[str, str]:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".strateg-doctor-", delete=True):
            pass
    except OSError as exc:
        return ERR, f"{folder} ({exc})"
    return OK, str(folder)


def _report(name: str, level: str, detail: str) -> str:
    print(f"[{_MARKS[level]}] {name}: {detail}")
    return level


def _gitleaks_line() -> tuple[str, str, str]:
    """`gitleaks` нужен `pack` как второй сканер секретов и коммитам; встроенные шаблоны работают без него."""
    exe = shutil.which("gitleaks")
    if exe:
        return "gitleaks", OK, exe
    return (
        "gitleaks",
        WARN,
        "не найден; `strateg pack` ищет секреты встроенными шаблонами; перед коммитом сканер лучше поставить",
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Проверка среды: Python, uv, git, SQLite, gitleaks, кодировка, запись в `projects/`.

    Ошибки дают код 1. Предупреждения (нет git, нет gitleaks, не UTF-8 в консоли) код не меняют.
    """
    levels = []
    levels.append(_report("Python", OK if sys.version_info >= (3, 12) else ERR, platform.python_version()))
    uv = shutil.which("uv")
    levels.append(_report("uv", OK if uv else ERR, uv or "не найден в PATH"))
    git = shutil.which("git")
    levels.append(
        _report("git", OK, git)
        if git
        else _report(
            "git", WARN, "не найден; для работы с проектом не обязателен, но нужен для истории изменений"
        )
    )
    levels.append(_report(*_gitleaks_line()))
    console = getattr(args, "console", None) or detect_console_encoding()
    levels.append(_report("Кодировка консоли", *console))
    levels.append(_report("SQLite", *getattr(args, "sqlite", None) or search.sqlite_status()))
    root = find_repo_root()
    if root is None:
        levels.append(_report("Запись в projects/", ERR, "не найден корень репозитория (pyproject.toml)"))
    elif not (root / "projects").is_dir():
        levels.append(_report("Запись в projects/", ERR, f"нет папки {root / 'projects'}"))
    else:
        levels.append(_report("Запись в projects/", *_check_writable(root / "projects")))
    for note in hookflags.notes(os.environ):  # заданные STRATEG_HOOKS и STRATEG_DEV видны сразу
        levels.append(_report("Хуки", WARN, note))
    return 1 if ERR in levels else 0


class _Parser(argparse.ArgumentParser):
    """Ошибки использования — по-русски, код возврата 2."""

    def error(self, message: str) -> NoReturn:
        message = re.sub(
            r"the following arguments are required: (.*)", r"не указаны обязательные аргументы: \1", message
        )
        message = re.sub(r"invalid choice: (.*)", r"недопустимое значение: \1", message)
        message = re.sub(r"\(choose from (.*)\)", r"(допустимо: \1)", message)
        message = re.sub(r"unrecognized arguments: (.*)", r"неизвестные аргументы: \1", message)
        message = re.sub(r"expected one argument", "у флага нет значения", message)
        message = re.sub(r"invalid int value: (.*)", r"нужно целое число, а получено: \1", message)
        message = re.sub(r"invalid float value: (.*)", r"нужно число, а получено: \1", message)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: ошибка использования: {message}", file=sys.stderr)
        raise SystemExit(2)


def _common(*, json_out: bool = True) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project", default=argparse.SUPPRESS, help="проект: номер или папка (главнее активного)"
    )
    if json_out:
        common.add_argument("--json", action="store_true", help="машинный вывод")
    return common


def _by_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--by",
        choices=["агент", "пользователь"],
        default="агент",
        help="кто правит: «пользователь» — правка человека (событие «исправление»)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="strateg", description="CLI агента «Стратег»")
    parser.add_argument("--version", action="version", version=f"strateg {__version__}")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    sub.add_parser("doctor", help="проверить среду").set_defaults(func=cmd_doctor)
    common = _common()

    def add(name: str, handler, help_: str, parent: argparse.ArgumentParser | None = None):
        p = (parent or sub).add_parser(name, parents=[common], help=help_)
        p.set_defaults(func=commands.run, handler=handler)
        return p

    p = add("new", commands.cmd_new, "создать проект и сделать его активным")
    p.add_argument("id", help="номер проекта: латинские строчные буквы, цифры, «-» и «_»")
    p.add_argument(
        "--mode", choices=["автопилот", "пилот"], default="автопилот", help="режим (по умолчанию автопилот)"
    )
    p.add_argument("--region", help="регион по умолчанию для fetch, например RU-MOW")

    p = add("use", commands.cmd_use, "выбрать активный проект")
    p.add_argument("id")

    p = add("status", commands.cmd_status, "статус проекта")
    shape = p.add_mutually_exclusive_group()
    shape.add_argument("--line", action="store_true", help="одна строка статуса для вставки в ответ")
    shape.add_argument(
        "--brief",
        action="store_true",
        help="сводка «где остановились» (до 10 строк); при продолжении работы записывает "
        "«прервано» и «изменено вне агента»",
    )
    p.add_argument(
        "--peek",
        action="store_true",
        help="с --brief: та же сводка без записи событий (середина сеанса, например после сжатия контекста)",
    )

    step = add("step", commands.cmd_step_start, "начало и конец шага")
    step_sub = step.add_subparsers(dest="step_command", required=True, parser_class=_Parser)
    p = step_sub.add_parser("start", parents=[common], help="начать шаг")
    p.set_defaults(func=commands.run, handler=commands.cmd_step_start)
    p.add_argument("step", help="шаг: 1, 2а, 2б, 3 … 8")
    p = step_sub.add_parser("done", parents=[common], help="отметить шаг выполненным")
    p.set_defaults(func=commands.run, handler=commands.cmd_step_done)
    p.add_argument("step")
    p.add_argument("--next", help="что делать дальше (одна фраза)")
    p = step_sub.add_parser("fail", parents=[common], help="отметить шаг ошибкой")
    p.set_defaults(func=commands.run, handler=commands.cmd_step_fail)
    p.add_argument("step")
    p.add_argument("--reason", required=True, help="что пошло не так")

    gate = add("gate", commands.cmd_gate_open, "ворота: открыть и ответить")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True, parser_class=_Parser)
    p = gate_sub.add_parser("open", parents=[common], help="открыть ворота")
    p.set_defaults(func=commands.run, handler=commands.cmd_gate_open)
    p.add_argument("gate", help="В1…В5")
    p.add_argument("--questions", type=int, required=True, help="сколько вопросов задано человеку")
    p = gate_sub.add_parser("answer", parents=[common], help="записать ответ человека")
    p.set_defaults(func=commands.run, handler=commands.cmd_gate_answer)
    p.add_argument("gate")
    p.add_argument("--answer", required=True, help="ответ человека")

    p = add("mode", commands.cmd_mode, "сменить режим работы")
    p.add_argument("mode", choices=["автопилот", "пилот"])

    p = add("stop", commands.cmd_stop, "записать внеплановую остановку")
    p.add_argument("--reason", required=True, help="причина остановки")

    source = add("source", commands.cmd_source_confirm, "источники")
    source_sub = source.add_subparsers(dest="source_command", required=True, parser_class=_Parser)
    p = source_sub.add_parser(
        "confirm", parents=[common], help="человек подтвердил, что материал общедоступный"
    )
    p.set_defaults(func=commands.run, handler=commands.cmd_source_confirm)
    p.add_argument("id", help="номер источника, например И-1")

    p = add("fetch", commands.cmd_fetch, "скачать страницу или PDF и сохранить снимок")
    p.add_argument("url", help="адрес http или https")
    p.add_argument("--region", help="код региона, если содержание зависит от региона")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="запросить страницу заново, минуя снимок проекта: тот же текст — отметка перепроверки, "
        "другой — новый источник со ссылкой на прежний",
    )

    p = add("add", commands.cmd_add, "зарегистрировать материал из inputs/ или подтвердить замены")
    p.add_argument("path", nargs="?", help="файл в папке inputs/ проекта")
    p.add_argument(
        "--kind",
        help="вид материала, если он не по расширению: «интервью» для расшифровки, "
        "«заметка пользователя» для текста с незнакомым расширением",
    )
    p.add_argument(
        "--confirm",
        metavar="И-n",
        help="человек подтвердил замены персональных данных (или что на изображении их нет)",
    )

    budget = add("budget", commands.cmd_budget_check, "бюджет проекта")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True, parser_class=_Parser)
    p = budget_sub.add_parser("set", parents=[common], help="задать бюджет проекта")
    p.set_defaults(func=commands.run, handler=commands.cmd_budget_set)
    p.add_argument("--usd", type=float, help="лимит денег, USD")
    p.add_argument("--minutes", type=float, help="лимит времени работы агента, минуты")
    p = budget_sub.add_parser("check", parents=[common], help="можно ли платное действие (0 — да, 1 — нет)")
    p.set_defaults(func=commands.run, handler=commands.cmd_budget_check)
    p.add_argument("--action", help="что собираются делать; «выпуск» берёт резерв бюджета")

    p = add("pack", commands.cmd_pack, "упаковать проект в архив .zip без секретов и исходников с ПДн")
    p.add_argument("--out", help="куда положить архив (по умолчанию — рядом с проектами, <id>.zip)")

    claim = add("claim", commands.cmd_claim_list, "утверждения")
    claim_sub = claim.add_subparsers(dest="claim_command", required=True, parser_class=_Parser)
    p = claim_sub.add_parser("add", parents=[common], help="записать утверждение или список из YAML или JSON")
    p.set_defaults(func=commands.run, handler=commands.cmd_claim_add)
    p.add_argument("--file", required=True, help="файл YAML или JSON; «-» — читать со стандартного ввода")
    p = claim_sub.add_parser("replace", parents=[common], help="записать утверждение вместо прежнего")
    p.set_defaults(func=commands.run, handler=commands.cmd_claim_replace)
    p.add_argument("id", help="заменяемое утверждение, например У-3")
    p.add_argument(
        "--file", required=True, help="файл YAML или JSON с новым утверждением; «-» — стандартный ввод"
    )
    _by_flag(p)
    p = claim_sub.add_parser("revoke", parents=[common], help="отозвать утверждение")
    p.set_defaults(func=commands.run, handler=commands.cmd_claim_revoke)
    p.add_argument("id", help="отзываемое утверждение, например У-3")
    p.add_argument("--reason", required=True, help="почему утверждение неверно")
    _by_flag(p)
    p = claim_sub.add_parser("list", parents=[common], help="список утверждений")
    p.set_defaults(func=commands.run, handler=commands.cmd_claim_list)
    p.add_argument("--type", choices=["факт", "гипотеза", "прогноз", "допущение", "синтетика"])
    p.add_argument("--step", help="только утверждения этого шага: 1, 2а, 2б, 3 … 8")
    p.add_argument("--plan-question", help="только утверждения этого вопроса плана, например ВП-2")
    p.add_argument("--status", choices=["действует", "заменено", "отозвано"])

    p = add("write", commands.cmd_write, "записать файлы выхода (рамка, план, пробелы, варианты …)")
    p.add_argument(
        "--file",
        required=True,
        help="файл YAML или JSON: ключи — frame, plan, gaps, options, promises, verification, decisions; "
        "«-» — стандартный ввод",
    )
    _by_flag(p)

    event = add("event", commands.cmd_event_error, "события журнала")
    event_sub = event.add_subparsers(dest="event_command", required=True, parser_class=_Parser)
    p = event_sub.add_parser("error", parents=[common], help="записать ошибку из скилла или хука")
    p.set_defaults(func=commands.run, handler=commands.cmd_event_error)
    p.add_argument("--message", required=True, help="что произошло")
    p.add_argument("--step", help="шаг; по умолчанию текущий")
    p.add_argument("--tool", help="инструмент, который дал ошибку")

    rub = add("rubric", rubric.cmd_rubric_check, "проверка полноты по рубрике")
    rub_sub = rub.add_subparsers(dest="rubric_command", required=True, parser_class=_Parser)
    p = rub_sub.add_parser(
        "check",
        parents=[common],
        help="признаки части А, которые к шагу уже должны выполняться (0 — все «да», 1 — есть «нет»)",
    )
    p.set_defaults(func=commands.run, handler=rubric.cmd_rubric_check)
    p.add_argument("--step", help="шаг: 1, 2а, 2б, 3 … 8 (по умолчанию текущий из state.yaml)")
    p = rub_sub.add_parser(
        "record",
        parents=[common],
        help="записать ответы части Б, прогнать часть А и вынести итог (коды выхода 0, 1, 2)",
        description="Записать ответы части Б, прогнать часть А на шаге проекта и вынести итог по pass_rule.\n"
        "Работает с шага 6 (проверка полноты).",
        epilog="Коды выхода:\n"
        "  0  итог «пройдено»\n"
        "  1  итог «не пройдено»: completeness.yaml и событие журнала записаны\n"
        "  2  ошибка входа (формат ответов, нет --by, шаг раньше 6): ничего не пишется\n"
        "В --json поле ok равно «пройдено».",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=commands.run, handler=rubric.cmd_rubric_record)
    p.add_argument(
        "--file",
        required=True,
        help="файл YAML или JSON: список {id, answer, where, why}; «-» — стандартный ввод",
    )
    p.add_argument(
        "--by",
        choices=["критик", "агент"],
        help="кто отвечал на часть Б, обязательно: «критик» — независимая проверка, «агент» — ответы дал "
        "сам агент, проверка не независимая (видно в файле и в выводе); значения по умолчанию нет",
    )

    p = add("route", routing.cmd_route, "модель и effort роли; профиль проекта")
    p.add_argument("role", nargs="?", help="роль из config/routing.yaml, например critic")
    p.add_argument("--reason", help="причина выбора (в журнал)")
    p.add_argument("--upgrade", action="store_true", help="повысить уровень по условию из таблицы")
    p.add_argument(
        "--profile", choices=["обычный", "экономнее", "глубже"], help="профиль проекта вместо выбора роли"
    )

    add("check", commands.cmd_check, "проверить активный проект")

    p = add("search", search.cmd_search, "найти в проекте и методике: утверждения, снимки, материалы")
    p.add_argument("query", help="слова запроса; падежи и числа учитываются")
    p.add_argument(
        "--also",
        action="append",
        metavar="ФОРМУЛИРОВКА",
        help="ещё одна формулировка того же вопроса (синонимы); можно повторять",
    )
    p.add_argument(
        "--in",
        dest="areas",
        action="append",
        choices=[*search.AREAS, search.ALL],
        help="где искать; можно повторять (по умолчанию: утверждения, источники, материалы)",
    )
    p.add_argument("--limit", type=int, default=search.DEFAULT_LIMIT, help="сколько мест показать")
    p.add_argument(
        "--only-active",
        action="store_true",
        help="скрыть отозванные и заменённые утверждения (по умолчанию они идут последними)",
    )

    p = add(
        "due",
        followup.cmd_due,
        "что решать сейчас: срок пересмотра, условия отказа, устаревшие основания, обязательства",
    )
    p.add_argument("--today", help="дата отсчёта ГГГГ-ММ-ДД (по умолчанию сегодня); нужна для повторяемости")

    p = add("impact", followup.cmd_impact, "что задевает новое сведение: источник И-n или утверждение У-n")
    p.add_argument("id", help="источник или утверждение, например И-2 или У-13")

    metric = add("metric", followup.cmd_metric_add, "значения показателей, которые внёс человек")
    metric_sub = metric.add_subparsers(dest="metric_command", required=True, parser_class=_Parser)
    p = metric_sub.add_parser(
        "add",
        parents=[common],
        help="записать значение показателя строки плана проверки и сравнить его с порогами условий",
    )
    p.set_defaults(func=commands.run, handler=followup.cmd_metric_add)
    p.add_argument("row", help="строка плана проверки, например ПП-2")
    p.add_argument("--value", required=True, help="число с единицей, как в пороге: «18 %%», «1500 ₽»")
    p.add_argument("--date", required=True, help="на какую дату значение, ГГГГ-ММ-ДД")
    p.add_argument("--note", help="откуда значение (выгрузка, отчёт)")

    p = add("diff", followup.cmd_diff, "что изменилось после выпуска: было → стало")
    p.add_argument("--release", type=int, help="номер выпуска (по умолчанию последний)")

    cost = add("cost", commands.cmd_cost_add, "учёт стоимости")
    cost_sub = cost.add_subparsers(dest="cost_command", required=True, parser_class=_Parser)
    p = cost_sub.add_parser("add", parents=[common], help="записать стоимость")
    p.set_defaults(func=commands.run, handler=commands.cmd_cost_add)
    p.add_argument("--session", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--usd", type=float)
    p.add_argument("--source", choices=["оценка", "измерено"], help="точность --usd (по умолчанию оценка)")
    for flag in ("input", "output", "cache-read", "cache-write-5m", "cache-write-1h"):
        p.add_argument(f"--{flag}", type=int, default=0, help=f"токены: {flag}")
    p.add_argument("--total", action="store_true", help="итог сессии, а не одно сообщение")
    p.add_argument(
        "--message-id",
        help="id сообщения; при повторе команды после сбоя передавайте тот же id, "
        "иначе расход удвоится (без id запись всегда новая)",
    )
    p.add_argument("--environment", default="claude-code", help="среда (по умолчанию claude-code)")
    p = cost_sub.add_parser(
        "report",
        parents=[common],
        help="стоимость проекта по ролям, шагам и моделям; --json — машинный вывод",
    )
    p.set_defaults(func=commands.run, handler=costreport.cmd_cost_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    console = detect_console_encoding()  # до reconfigure, иначе проверка всегда «OK»
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # ошибка использования (2) или --help и --version (0)
        return int(exc.code or 0)
    args.console = console
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
