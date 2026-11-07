"""Тесты скиллов и `AGENTS.md`: форма, длина, ссылки, команды CLI, примеры записи и чистота текста.

Скилл — это инструкция агенту, и она ломается тихо: команда разбирается не так, пример не проходит
проверку модели, ссылка ведёт в пустоту. Эти тесты ловят такое без запуска агента.
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest
import yaml

from strateg.cli import build_parser
from strateg.commands import GATES, STEP_NAMES, ClaimIn
from strateg.outputs import KINDS, parse_input, validate
from test_no_internal import FORBIDDEN, PATTERNS

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
AGENTS = ROOT / "AGENTS.md"

MAX_DESCRIPTION = 1024
MAX_SKILL_BODY_LINES = 500
MAX_AGENTS_LINES = 200

# Пять скиллов шагов; маршрутизатор `strateg` и остальные добавляются отдельно и в список не входят.
STEP_SKILLS = {
    # скилл: (шаги, ворота, виды файлов выхода)
    "frame": (["1"], ["В1"], ["frame"]),
    "research": (["2а", "2б", "3"], ["В2", "В3"], ["plan", "gaps"]),
    "options": (["4"], ["В4"], ["options", "gaps"]),
    "strategy": (["5", "6"], [], ["promises", "verification"]),
    "package": (["7"], ["В5"], ["decisions"]),
}

# Строки учебного кейса: ими не должны пахнуть ни `AGENTS.md`, ни скиллы (список копии для прогона
# плюс слова предметной области, которых на новой задаче в документах быть не должно).
CASE_STRINGS = (
    "Защита близких",
    "Семейная безопасность",
    "fraud-protection",
    "expected.md",
    "мошенн",
    "МегаФон",
    "Моя безопасность",
    "абонент",
    "тариф",
)
CASE_WORDS = ("Ева",)
DOMAIN_WORDS = ("абонент", "тариф")  # слова области по умолчанию; в `domain-words.yaml` допустимы
# Структура кейса: развилка, цепочка «пробел → возражение → показатель», формулировки рамки.
CASE_PHRASES = (
    "отдельной подпиской",
    "включить в тарифы",
    "включить защиту",
    "после пробного периода",
    "доля отключений",
    "рост отключений",
    "доработка самой услуги",
    "рекомендация, которую можно защитить",
    "защитимая рекомендация",
)
CASES = ROOT / "evals" / "cases"
FIXTURE_PROJECT = ROOT / "tests" / "fixtures" / "project_valid"


def squeeze(text: str) -> str:
    """Текст без пробелов в тысячных разрядах: «1 499 ₽» и «1499 ₽» — одно и то же."""
    return re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))", "", text)


def case_prices() -> set[str]:
    """Цены из карточки кейса и ориентиров судьи: числа перед знаком рубля."""
    prices: set[str] = set()
    for path in [*CASES.glob("*/card.yaml"), *CASES.glob("*/expected.md")]:
        for m in re.finditer(r"(\d+)\s*(?:₽|руб)", squeeze(path.read_text(encoding="utf-8"))):
            prices.add(m.group(1))
    return prices


def case_thresholds() -> set[str]:
    """Пороги примера проекта, собранного по кейсу: их нельзя копировать в скиллы."""
    found: set[str] = set()
    for path in FIXTURE_PROJECT.glob("*.yaml"):
        for m in re.finditer(r"threshold:\s*(.+)", path.read_text(encoding="utf-8")):
            found.add(re.sub(r"\s+", "", m.group(1)).casefold())
    return found


# Файлы репозитория, на которые ссылаются тексты.
REPO_ROOTS = (".claude", "knowledge", "docs", "schemas", "config", "src", "scripts", "tests", "evals")

FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```", re.S | re.M)
SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def skill_files() -> list[Path]:
    return sorted(SKILLS.rglob("*.md")) if SKILLS.is_dir() else []


def skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if p.is_dir()) if SKILLS.is_dir() else []


KNOWLEDGE = ROOT / "knowledge"


def knowledge_files() -> list[Path]:
    return sorted(p for p in KNOWLEDGE.rglob("*") if p.is_file())


def all_texts() -> list[Path]:
    return [AGENTS, *skill_files()]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def split_skill(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert m, f"{rel(path)}: нет блока frontmatter между строками ---"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict), f"{rel(path)}: frontmatter не словарь"
    return meta, m.group(2)


# ---------------------------------------------------------------------------
# Форма и длина
# ---------------------------------------------------------------------------


def test_step_skills_exist():
    missing = [name for name in STEP_SKILLS if not (SKILLS / name / "SKILL.md").is_file()]
    assert not missing, f"нет скиллов шагов: {missing}"


@pytest.mark.parametrize("folder", skill_dirs(), ids=lambda p: p.name)
def test_skill_frontmatter(folder):
    skill = folder / "SKILL.md"
    assert skill.is_file(), f"в {rel(folder)} нет SKILL.md"
    meta, _ = split_skill(skill)
    assert set(meta) == {"name", "description"}, (
        f"{rel(skill)}: поля {sorted(meta)}, нужны только name и description"
    )
    assert meta["name"] == folder.name, f"{rel(skill)}: name «{meta['name']}» не равен папке «{folder.name}»"
    assert re.fullmatch(r"[a-z][a-z0-9-]*", meta["name"]), f"{rel(skill)}: name — латиница, цифры и дефис"
    desc = meta["description"]
    assert isinstance(desc, str) and desc.strip(), f"{rel(skill)}: пустое description"
    assert len(desc) <= MAX_DESCRIPTION, (
        f"{rel(skill)}: description {len(desc)} знаков, предел {MAX_DESCRIPTION}"
    )


@pytest.mark.parametrize("folder", skill_dirs(), ids=lambda p: p.name)
def test_skill_body_is_short(folder):
    _, body = split_skill(folder / "SKILL.md")
    lines = len(body.strip("\n").splitlines())
    assert lines <= MAX_SKILL_BODY_LINES, (
        f"{rel(folder / 'SKILL.md')}: {lines} строк, предел {MAX_SKILL_BODY_LINES}"
    )


def test_agents_md_is_short():
    lines = len(AGENTS.read_text(encoding="utf-8").splitlines())
    assert lines <= MAX_AGENTS_LINES, f"AGENTS.md: {lines} строк, предел {MAX_AGENTS_LINES}"


# ---------------------------------------------------------------------------
# Ссылки на файлы
# ---------------------------------------------------------------------------


def file_refs(text: str) -> set[str]:
    """Пути репозитория из обратных кавычек и из ссылок Markdown."""
    found: set[str] = set()
    for span in SPAN.findall(FENCE.sub("", text)):
        token = span.strip()
        if re.fullmatch(r"[\w./-]+", token) and token.split("/")[0] in REPO_ROOTS:
            found.add(token)
        elif re.fullmatch(r"(AGENTS|CLAUDE|README|CHANGELOG)\.md", token):
            found.add(token)
    return found


@pytest.mark.parametrize("path", all_texts(), ids=rel)
def test_file_references_exist(path):
    text = path.read_text(encoding="utf-8")
    broken = []
    for ref in sorted(file_refs(text)):
        if not (ROOT / ref).exists():
            broken.append(ref)
    for link in LINK.findall(FENCE.sub("", text)):
        target = link.split("#")[0]
        if not target or re.match(r"[a-z]+:", target):
            continue
        if not (path.parent / target).exists():
            broken.append(link)
    assert not broken, f"{rel(path)}: нет файлов {broken}"


# ---------------------------------------------------------------------------
# Команды CLI
# ---------------------------------------------------------------------------


def _placeholder(inner: str) -> str:
    """Подстановка значения вместо `<…>` так, чтобы команду разобрал argparse."""
    inner = inner.strip()
    if "|" in inner:
        return inner.split("|")[0].strip()
    if re.fullmatch(r"[Nn]|\d+", inner):
        return "1"
    if inner.startswith("В") and "…" in inner:
        return "В1"
    if inner == "шаг":
        return "1"
    if m := re.fullmatch(r"([А-Яа-я]+)-n", inner):
        return f"{m.group(1)}-1"
    return "x"


def normalize(command: str) -> list[str]:
    """Команда из текста скилла -> список аргументов после `strateg`."""
    command = re.sub(r"\[[^\]]*\]", "", command)  # необязательные части
    command = re.sub(r"<([^<>]*)>", lambda m: _placeholder(m.group(1)), command)
    tokens = shlex.split(command)
    assert tokens[:3] == ["uv", "run", "strateg"] or tokens[:1] == ["strateg"], tokens
    return tokens[3:] if tokens[0] == "uv" else tokens[1:]


def find_commands(text: str) -> tuple[list[str], list[str]]:
    """Команды `uv run strateg …` (разбираются целиком) и упоминания `strateg …` (только имя команды)."""
    full: list[str] = []
    mentions: list[str] = []
    for _info, body in FENCE.findall(text):
        for line in body.splitlines():
            if line.lstrip().startswith("uv run strateg"):
                full.append(line.strip())
    for span in SPAN.findall(FENCE.sub("", text)):
        span = span.strip()
        if span.startswith("uv run strateg"):
            full.append(span)
        elif re.match(r"strateg [a-z]", span):
            mentions.append(span)
    return full, mentions


def run_parser(argv: list[str]) -> tuple[int, str]:
    """Код выхода argparse и последняя строка его сообщения об ошибке; ничего не печатает."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            build_parser().parse_args(argv)
        except SystemExit as exc:
            lines = err.getvalue().strip().splitlines()
            return int(exc.code or 0), lines[-1] if lines else ""
    return 0, ""


def bad_values(argv: list[str]) -> list[str]:
    """Значения, которые argparse пропускает, а CLI отклонит: шаг вне списка, ворота вне В1–В5."""
    problems = []
    if argv[:2] in (["step", "start"], ["step", "done"], ["step", "fail"]) and argv[2] not in STEP_NAMES:
        problems.append(f"шаг «{argv[2]}» не бывает")
    if argv[:2] in (["gate", "open"], ["gate", "answer"]) and argv[2] not in GATES:
        problems.append(f"ворота «{argv[2]}» не бывают")
    if "--step" in argv and argv[argv.index("--step") + 1] not in STEP_NAMES:
        problems.append(f"шаг «{argv[argv.index('--step') + 1]}» не бывает")
    if "--plan-question" in argv and not re.fullmatch(r"ВП-\d+", argv[argv.index("--plan-question") + 1]):
        problems.append("вопрос плана — вида ВП-1, буквы русские")
    return problems


@pytest.mark.parametrize("path", all_texts(), ids=rel)
def test_commands_are_parsed_by_the_cli(path):
    full, mentions = find_commands(path.read_text(encoding="utf-8"))
    bad = []
    for command in full:
        argv = normalize(command)
        code, message = run_parser(argv)
        if code != 0:
            bad.append(f"{command} -> {message}")
        bad.extend(f"{command} -> {problem}" for problem in bad_values(argv))
    for mention in mentions:
        code, _ = run_parser([*normalize(mention), "--help"])
        if code != 0:
            bad.append(f"{mention} -> такой команды нет")
    assert not bad, f"{rel(path)}: команды не разбираются:\n" + "\n".join(bad)


def test_every_skill_has_commands():
    for name in STEP_SKILLS:
        full, _ = find_commands((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))
        assert len(full) >= 5, f"{name}: мало команд CLI"


def test_normalize_catches_a_broken_command():
    """Контроль самого теста: опечатка в команде не проходит."""
    assert run_parser(normalize("uv run strateg step start 1"))[0] == 0
    assert run_parser(normalize("uv run strateg step begin 1"))[0] != 0
    assert run_parser(normalize("uv run strateg write"))[0] != 0  # нет --file
    assert run_parser(normalize("uv run strateg claim revoke У-1 --by пользователь"))[0] != 0  # нет --reason
    assert run_parser(normalize("uv run strateg gate open <В1…В5> --questions <N>"))[0] == 0
    assert run_parser(normalize("uv run strateg mode <пилот|автопилот>"))[0] == 0
    assert bad_values(normalize("uv run strateg gate open В7 --questions 1"))
    assert bad_values(normalize("uv run strateg step start 9"))
    assert bad_values(normalize("uv run strateg claim list --step zz"))
    assert not bad_values(normalize("uv run strateg step done 2б --next 'x'"))
    assert not bad_values(normalize("uv run strateg claim list --step 2б --plan-question ВП-<n>"))


# ---------------------------------------------------------------------------
# Что каждый скилл обязан сказать
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STEP_SKILLS)
def test_skill_covers_its_steps_gates_and_files(name):
    steps, gates, kinds = STEP_SKILLS[name]
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    for step in steps:
        assert f"uv run strateg step start {step}" in text, f"{name}: нет step start {step}"
        assert f"uv run strateg step done {step}" in text, f"{name}: нет step done {step}"
    for gate in gates:
        assert f"uv run strateg gate open {gate} --questions" in text, f"{name}: нет gate open {gate}"
    for kind in kinds:
        assert re.search(rf"\b{kind}\b", text), f"{name}: не сказано, как записать «{kind}»"
    assert "uv run strateg write --file" in text, f"{name}: нет записи через write"


def test_frame_skill_follows_the_gate_order():
    """Рамка: черновик, ворота, правка, ответ на В1 — в таком порядке."""
    text = (SKILLS / "frame" / "SKILL.md").read_text(encoding="utf-8")
    order = ["uv run strateg write --file", "gate open В1", "gate answer В1", "step done 1"]
    positions = [text.find(x) for x in order]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), "порядок: write → gate open В1 → gate answer В1 → step done 1"
    assert "--by пользователь" in text


def test_options_skill_has_the_chosen_option_step_and_loop_back():
    text = (SKILLS / "options" / "SKILL.md").read_text(encoding="utf-8")
    assert "chosen" in text and "сначала закрой" in text and "другой" in text


def test_research_skill_asks_for_two_searches():
    text = (SKILLS / "research" / "SKILL.md").read_text(encoding="utf-8")
    assert "не меньше двух поисков" in text
    assert "claim list --step 2б --plan-question" in text


def test_strategy_skill_carries_the_lessons_of_the_second_run():
    text = (SKILLS / "strategy" / "SKILL.md").read_text(encoding="utf-8")
    assert "[О-n]" in text  # метки обещаний
    assert "Таблица обещаний" in text
    assert "не повторяет" in text  # пре-мортем не повторяет возражения
    assert "completeness.md" in text


def test_research_skill_lists_all_five_metrics():
    text = (SKILLS / "research" / "SKILL.md").read_text(encoding="utf-8")
    for kind in ("охват", "конверсия", "удержание и отток", "выручка и затраты", "качество продукта"):
        assert kind in text, kind


# ---------------------------------------------------------------------------
# Примеры записи проходят проверку моделей
# ---------------------------------------------------------------------------

CLAIM_TYPES = {"факт", "гипотеза", "прогноз", "допущение", "синтетика"}


def yaml_examples() -> list[tuple[str, str, str]]:
    """(файл, пометка, текст) для каждого блока yaml в скиллах."""
    found = []
    for path in skill_files():
        for info, body in FENCE.findall(path.read_text(encoding="utf-8")):
            words = info.split()
            if words and words[0] == "yaml":
                found.append((rel(path), words[1] if len(words) > 1 else "", body))
    return found


def check_example(data) -> tuple[str, list[str]]:
    """Вид примера и ошибки: вход `write` разбирает код CLI, утверждение — модель входа `claim add`."""
    if isinstance(data, dict) and data and set(data) <= set(KINDS):
        problems = []
        try:
            parsed = parse_input(data)
        except Exception as exc:  # noqa: BLE001 - показываем причину целиком
            return "write", [f"оболочка входа: {exc}"]
        for kind, content in parsed.items():
            _, errors = validate(KINDS[kind], content)
            problems.extend(f"вид {kind}: {e}" for e in errors)
        return "write", problems
    items = data if isinstance(data, list) else [data]
    if items and all(isinstance(i, dict) and "type" in i for i in items):
        problems = []
        for item in items:
            try:
                ClaimIn.model_validate(item)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"утверждение: {exc}")
        return "claim", problems
    return "unknown", ["пример не вход write и не утверждение: поставь пометку `yaml fragment` или исправь"]


def test_yaml_examples_pass_the_cli_models():
    kinds: set[str] = set()
    claim_types: set[str] = set()
    problems: list[str] = []
    for file, tag, body in yaml_examples():
        if tag == "fragment":
            continue
        data = yaml.safe_load(body)
        what, errors = check_example(data)
        problems.extend(f"{file}: {e}" for e in errors)
        if what == "write":
            kinds |= set(data)
        elif what == "claim":
            claim_types |= {i["type"] for i in (data if isinstance(data, list) else [data])}
    assert not problems, "\n".join(problems)
    # примеры не потерялись: образец полного входа есть у каждого вида, образец утверждения у каждого типа
    assert kinds == set(KINDS), f"нет образца входа write для видов {sorted(set(KINDS) - kinds)}"
    assert {"факт", "гипотеза", "прогноз", "допущение"} <= claim_types


def test_example_checker_catches_broken_examples():
    """Контроль самого теста: оболочка, тип и вложенность проверяются."""
    assert check_example({"promises": [{"id": "О-1"}]})[1]  # список вместо набора полей
    assert check_example({"frame": {"decision": "x"}})[1]  # поля рамки не по схеме
    assert check_example([{"text": "x", "type": "fact", "subject": "y"}])[1]  # нет такого типа
    assert check_example({"a": 1})[0] == "unknown"
    assert not check_example(
        {
            "promises": {
                "promises": [
                    {
                        "id": "О-1",
                        "text": "x",
                        "capability": "y",
                        "conditions": "z",
                        "status": "не подтверждено",
                    }
                ]
            }
        }
    )[1]


# ---------------------------------------------------------------------------
# Чистота текста
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*all_texts(), *knowledge_files()], ids=rel)
def test_no_case_strings(path):
    text = path.read_text(encoding="utf-8")
    low = text.casefold()
    # Список слов области — единственное место, где «абонент» и «тариф» уместны: ради них он и нужен.
    domain = DOMAIN_WORDS if path.name == "domain-words.yaml" else ()
    found = [s for s in CASE_STRINGS if s.casefold() in low and s not in domain]
    found += [w for w in CASE_WORDS if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text)]
    found += [s for s in CASE_PHRASES if s.casefold() in low]
    squeezed = squeeze(text)
    found += [
        f"{price} ₽" for price in sorted(case_prices()) if re.search(rf"(?<!\d){price}\s*(?:₽|руб)", squeezed)
    ]
    flat = re.sub(r"\s+", "", low)
    found += [f"порог «{s}»" for s in sorted(case_thresholds()) if s in flat]
    assert not found, f"{rel(path)}: строки, цены и пороги учебного кейса {found}"


def test_case_lists_are_not_empty():
    """Контроль самого теста: карточка кейса и пример проекта читаются, цены и пороги найдены."""
    assert case_prices(), "в карточке кейса не нашлось цен: проверка чисел ничего не проверяет"
    assert case_thresholds(), "в примере проекта не нашлось порогов"


def test_phrase_table_lives_only_in_agents_md():
    """Таблица фраз управления — только в AGENTS.md; скиллы ссылаются на неё."""
    assert "| Фраза |" in AGENTS.read_text(encoding="utf-8")
    for path in skill_files():
        assert "| Фраза |" not in path.read_text(encoding="utf-8"), rel(path)


def test_first_turn_summary_is_described_in_one_place():
    """`status --brief` — раз на проект за чат: он в AGENTS.md и strateg, другие скиллы его не вызывают."""
    for path in skill_files():
        if path.parts[-2] == "strateg":
            continue
        assert "status --brief" not in path.read_text(encoding="utf-8"), rel(path)
    agents = AGENTS.read_text(encoding="utf-8")
    assert "один раз на проект за чат" in agents and "сразу после `use`" in agents
    assert "Повтор на том же проекте запрещён" in agents and "status --brief --peek" in agents


_ID = re.compile(r"^(И|У|ВП|П|Вар|О|ПП|Реш)-\d+$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_PAIR = re.compile(r"(?:^\s*(?:- )?|[{,] )([A-Za-z_]+): ([^{},\[\]\n][^,}\n]*)", re.M)


def _closed_values() -> set[str]:
    import typing

    from strateg import models

    out: set[str] = set()
    for name in dir(models):
        obj = getattr(models, name)
        if typing.get_origin(obj) is typing.Literal:
            out |= {v for v in typing.get_args(obj) if isinstance(v, str)}
    return out


def test_texts_in_write_samples_are_quoted():
    """Агент перенимает стиль образца: текст без кавычек ломается на запятой, двоеточии и `#`."""
    closed = _closed_values()
    bare = []
    for path in skill_files():
        for lang, body in FENCE.findall(path.read_text(encoding="utf-8")):
            if lang.strip() != "yaml" or path.name != "SKILL.md":
                continue
            for key, value in _PAIR.findall(body):
                v = value.strip()
                if v[:1] in ("'", '"', "|", ">") or not v or v in closed or v in ("true", "false"):
                    continue
                if not (_ID.match(v) or _NUMBER.match(v)):
                    bare.append(f"{rel(path)}: {key}: {v}")
    assert not bare, bare


@pytest.mark.parametrize("path", [*all_texts(), *knowledge_files()], ids=rel)
def test_no_internal_traces(path):
    text = path.read_text(encoding="utf-8")
    found = [label for label, needle in FORBIDDEN.items() if needle.lower() in text.lower()]
    found += [label for label, pattern in PATTERNS.items() if pattern.search(text)]
    for label, pattern in {
        "номер замечания IMP": re.compile(r"\bIMP-\d"),
        "номер вида R-n": re.compile(r"\bR-\d"),
        "номер задания N.N-N": re.compile(r"\b\d\.\d-\d\b"),
        "личная папка": re.compile(r"(?i)\\Users\\|/Users/|/home/\w+"),
    }.items():
        if pattern.search(text):
            found.append(label)
    assert not found, f"{rel(path)}: служебное {found}"


# ---------------------------------------------------------------------------
# Шаблоны документов, карточки методов, рубрика
# ---------------------------------------------------------------------------

TEMPLATES = KNOWLEDGE / "templates"
METHODS = KNOWLEDGE / "methods"
RUBRIC = KNOWLEDGE / "rubrics" / "completeness.yaml"
MAX_TEMPLATE_LINES = 40
MAX_CARD_LINES = 25  # без раздела «Источники»

# Разделы документов, как их перечисляли скиллы до шаблонов: по порядку. Шаблон не меняет смысл скилла.
TEMPLATE_HEADINGS = {
    "frame": ["Кратко", "Поля рамки", "Было"],
    "plan": ["Кратко", "Вопросы плана", "Исключено из плана", "Оценка пути"],
    "situation": ["Кратко", "Известно", "Где источники спорят", "Чего не знаем"],
    "options": [
        "Кратко",
        "Варианты",
        "Возражения",
        "Сравнение",
        "Рекомендация",
        "Пробелы, которые могут изменить выбор",
    ],
    "product-strategy": [
        "Кратко",
        "Аудитория и её задача",
        "Альтернативы",
        "Диагноз",
        "Направление",
        "Осознанные отказы",
        "Польза, затраты и доходы",
    ],
    "comms-strategy": [
        "Кратко",
        "Аудитория и ситуация",
        "Барьер",
        "Ключевые сообщения",
        "Роли каналов",
        "Таблица обещаний",
    ],
    "verification-plan": [
        "Кратко",
        "Ожидаемое изменение",
        "База сравнения",
        "Срок",
        "Нежелательные последствия",
        "Защитные показатели",
        "Условия отказа",
        "Строки плана",
    ],
    "one-pager": [
        "Кратко",
        "Рекомендация и почему",
        "Сильнейшее возражение и ответ",
        "Что заставит передумать",
        "Владелец и пересмотр",
    ],
    "report": [
        "Кратко",
        "Картина ситуации",
        "Варианты",
        "Стратегии",
        "План проверки",
        "Пробелы",
        "Решение",
    ],
    "run-report": [
        "Кратко",
        "Время и стоимость",
        "Сбои",
        "Подозрительные источники",
        "Помощь человека",
        "Проверка полноты",
        "Допущения",
        "Среда",
        "Ограничения этой версии",
        "Не сделано",
    ],
}
CARDS = [
    "frame-and-questions",
    "sources-and-evidence",
    "gaps",
    "users-and-alternatives",
    "options",
    "objections-and-premortem",
    "product-strategy",
    "communication-strategy",
    "validation-plan",
]


def headings(path: Path) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", path.read_text(encoding="utf-8"), re.M)


def test_ten_templates_exist():
    assert sorted(p.stem for p in TEMPLATES.glob("*.md")) == sorted(TEMPLATE_HEADINGS)


@pytest.mark.parametrize("name", TEMPLATE_HEADINGS)
def test_template_has_the_headings_of_its_document(name):
    path = TEMPLATES / f"{name}.md"
    assert headings(path) == TEMPLATE_HEADINGS[name], f"{rel(path)}: заголовки и их порядок"
    lines = len(path.read_text(encoding="utf-8").splitlines())
    assert lines <= MAX_TEMPLATE_LINES, f"{rel(path)}: {lines} строк, предел {MAX_TEMPLATE_LINES}"


@pytest.mark.parametrize("name", TEMPLATE_HEADINGS)
def test_template_only_describes(name):
    """Под каждым заголовком — одно описание в `<…>`: готовой фразы, образца и примера нет."""
    text = (TEMPLATES / f"{name}.md").read_text(encoding="utf-8")
    sections = re.split(r"^## .+$", text, flags=re.M)[1:]
    assert len(sections) == len(TEMPLATE_HEADINGS[name])
    for body in sections:
        lines = [line for line in body.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"{name}: под заголовком не одна строка"
        line = lines[0].removeprefix("- ")
        assert line.startswith("<") and line.endswith(">"), f"{name}: нет описания в <…>: {line[:40]}"
    assert headings(TEMPLATES / f"{name}.md")[0] == "Кратко"


@pytest.mark.parametrize("name", TEMPLATE_HEADINGS)
def test_a_skill_points_to_each_template(name):
    ref = f"knowledge/templates/{name}.md"
    holders = [p for p in skill_files() if ref in p.read_text(encoding="utf-8")]
    assert holders, f"ни один скилл не ссылается на {ref}"


def test_skills_do_not_repeat_the_template_sections():
    """Списки разделов живут в шаблонах: в скиллах строк вида «- «Кратко» — …» нет."""
    pattern = re.compile(r"^\s*-\s*«(Кратко|Известно|Варианты|Возражения|Сравнение|Рекомендация)»", re.M)
    for path in skill_files():
        assert not pattern.search(path.read_text(encoding="utf-8")), rel(path)


@pytest.mark.parametrize("name", CARDS)
def test_method_card_is_short_and_has_sources(name):
    path = METHODS / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    assert "\n## Источники\n" in text, f"{rel(path)}: нет раздела «Источники»"
    body, sources = text.split("\n## Источники\n", 1)
    assert len(body.rstrip().splitlines()) <= MAX_CARD_LINES, f"{rel(path)}: длиннее {MAX_CARD_LINES} строк"
    assert "07.11.2026" in sources, f"{rel(path)}: нет даты обращения"
    for part in ("## Правила", "## Что это проверяет", "## Не делай"):
        assert part in body, f"{rel(path)}: нет раздела «{part[3:]}»"
    entries = [line for line in sources.splitlines() if line.startswith("- ")]
    assert entries, f"{rel(path)}: нет ни одного источника"
    for line in entries:
        assert "http" in line or "карточку" in line, f"{rel(path)}: у источника нет адреса: {line[:60]}"


def test_method_cards_are_exactly_the_planned_nine():
    assert sorted(p.stem for p in METHODS.glob("*.md")) == sorted(CARDS)


@pytest.mark.parametrize("name", CARDS)
def test_a_skill_points_to_each_card_with_a_condition(name):
    pattern = re.compile(rf"Метод и ситуации — `knowledge/methods/{name}\.md`: прочитай, когда \S+")
    assert any(pattern.search(p.read_text(encoding="utf-8")) for p in skill_files()), name


def test_rubric_is_yaml_within_limits_and_quoted():
    text = RUBRIC.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert len(data["part_a"]) <= 30 and len(data["part_b"]) <= 15
    ids = [x["id"] for x in data["part_a"] + data["part_b"]]
    assert len(ids) == len(set(ids))
    key = r"(id|step|rule|question|no_means|source|pass_rule|answer_format|how_to_read)"
    bare = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(rf"\s*(?:- )?{key}: (.*)", line)
        if m and not m.group(2).startswith('"'):
            bare.append(f"{n}: {line.strip()[:50]}")
    assert not bare, bare


def test_rubric_sources_name_existing_cards():
    """В `source` — названия карточек методов, и такие карточки есть."""
    text = RUBRIC.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    name = r"[a-z]+(?:-[a-z]+)*"
    names: set[str] = set()
    for item in data["part_a"] + data["part_b"]:
        for m in re.finditer(rf"карточк\S* ({name}(?: и {name})*)", item["source"]):
            names |= set(m.group(1).split(" и "))
    assert names and names <= set(CARDS), sorted(names - set(CARDS))
    assert "Правило «" not in text


def test_rubric_section_names_match_the_templates():
    """Названия разделов в признаках — как в шаблонах."""
    text = RUBRIC.read_text(encoding="utf-8")
    known = {h for hs in TEMPLATE_HEADINGS.values() for h in hs}
    for name in re.findall(r"раздел\w* «([^»]+)»", text):
        assert name in known, f"в рубрике раздел «{name}», которого нет в шаблонах"
