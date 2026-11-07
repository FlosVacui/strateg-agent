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
from strateg.models import Decision, Option, PremortemCause, Promise, Recommendation, VerificationRow
from strateg.outputs import KINDS, validate
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

# Файлы репозитория, на которые ссылаются тексты; шаблоны пишет другое задание, их пока нет.
REPO_ROOTS = (".claude", "knowledge", "docs", "schemas", "config", "src", "scripts", "tests", "evals")
NOT_YET = ("knowledge/templates/",)

FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```", re.S | re.M)
SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def skill_files() -> list[Path]:
    return sorted(SKILLS.rglob("*.md")) if SKILLS.is_dir() else []


def skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if p.is_dir()) if SKILLS.is_dir() else []


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
        if ref.startswith(NOT_YET):
            continue
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


@pytest.mark.parametrize("path", all_texts(), ids=rel)
def test_commands_are_parsed_by_the_cli(path):
    full, mentions = find_commands(path.read_text(encoding="utf-8"))
    bad = []
    for command in full:
        code, message = run_parser(normalize(command))
        if code != 0:
            bad.append(f"{command} -> {message}")
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
    for needed in ("uv run strateg status --brief", "uv run strateg event error", "uv run strateg step fail"):
        assert needed in text, f"{name}: нет «{needed}»"


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

FRAGMENTS = {
    "option": Option,
    "recommendation": Recommendation,
    "promise": Promise,
    "row": VerificationRow,
    "premortem": PremortemCause,
    "decision": Decision,
}


def yaml_examples() -> list[tuple[str, str, str]]:
    """(файл, пометка, текст) для каждого блока yaml в скиллах."""
    found = []
    for path in skill_files():
        for info, body in FENCE.findall(path.read_text(encoding="utf-8")):
            words = info.split()
            if words and words[0] == "yaml":
                found.append((rel(path), words[1] if len(words) > 1 else "", body))
    return found


def test_yaml_examples_pass_the_models():
    checked: set[str] = set()
    problems: list[str] = []
    for file, tag, body in yaml_examples():
        data = yaml.safe_load(body)
        if tag in FRAGMENTS:
            items = data if isinstance(data, list) else [data]
            for item in items:
                try:
                    FRAGMENTS[tag].model_validate(item, by_name=False)
                except Exception as exc:  # noqa: BLE001 - показываем причину целиком
                    problems.append(f"{file}: пример «{tag}»: {exc}")
            checked.add(tag)
        elif isinstance(data, dict) and data and set(data) <= set(KINDS):
            for kind, content in data.items():
                _, errors = validate(KINDS[kind], content)
                problems.extend(f"{file}: вид {kind}: {e}" for e in errors)
                checked.add(kind)
    assert not problems, "\n".join(problems)
    # примеры не потерялись: проверено всё, что должно быть
    assert {"frame", "plan", "gaps"} <= checked
    assert set(FRAGMENTS) <= checked


# ---------------------------------------------------------------------------
# Чистота текста
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", all_texts(), ids=rel)
def test_no_case_strings(path):
    text = path.read_text(encoding="utf-8")
    low = text.casefold()
    found = [s for s in CASE_STRINGS if s.casefold() in low]
    found += [w for w in CASE_WORDS if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text)]
    assert not found, f"{rel(path)}: строки учебного кейса {found}"


@pytest.mark.parametrize("path", all_texts(), ids=rel)
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
