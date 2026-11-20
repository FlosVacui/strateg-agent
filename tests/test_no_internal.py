"""Тест-страж: в продукте нет служебных пометок команды, графа знаний, личных путей и имени автора,
номеров замечаний и заданий, ссылок на рабочую папку заданий, служебных слов, имён профилей
Claude и рабочих копий, личных адресов почты и имён участников, а также названия компании-спонсора
в фикстурах.

Имена участников в тесте не записаны: хранятся хеши sha256 слов, а тест хеширует слова файлов.
Строки поиска собраны из частей, чтобы тест не находил сам себя; самотесты держат каждое правило.
Проверяются файлы, которые попадут в репозиторий: отслеживаемые git и новые, не закрытые
`.gitignore`. Без git (например, из архива) — обход папки без `.venv` и `.git`.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = {
    "рабочая папка команды": "strateg" + "-" + "hq",
    "граф знаний": "graph" + "ify",
    "имя автора": "Flos" + "Vacui",
    "рабочая копия задания": "strateg" + "-wt",
    "профиль Claude": ".claude" + "-acc",
}
# Служебный адрес автора коммитов разрешён целиком; логин в других местах (например, в личной почте) — нет.
SERVICE_ADDRESS = "328751003+" + "Flos" + "Vacui" + "@users.noreply.github.com"
SERVICE_ADDRESS_RE = re.compile(re.escape(SERVICE_ADDRESS), re.IGNORECASE)
# Компания-спонсор: её названия не должно быть в страницах-образцах.
SPONSOR = ("мега" + "фон").lower()
# Имена участников: sha256 слова в нижнем регистре (слово — подряд идущие латинские буквы и цифры).
# Хеш, а не имя: само имя в продукте не нужно. Новое имя добавляют так:
# `python -c "import hashlib; print(hashlib.sha256(b'слово').hexdigest())"`.
NAME_HASHES = {
    "f735fb23ffb96815d2027bc60d5273c118fe90180d70ea7b5dfb8669dbbc427f",
    "a32341b5b616225aa409315ca2f789f023b0c71fb6a77c43cd954a44401f8ffb",
}
WORD = re.compile(r"[A-Za-z0-9]+")


def known_names(text: str, hashes: set[str] = NAME_HASHES) -> list[str]:
    """Слова текста, чей хеш — в списке имён; возвращает хеши, а не сами слова."""
    found = []
    for word in set(WORD.findall(text)):
        digest = hashlib.sha256(word.lower().encode()).hexdigest()
        if digest in hashes:
            found.append(digest[:8])
    return found


# Номера замечаний и заданий, личные пути, служебные слова: шаблоны, тоже собранные из частей.
# Вымышленное имя в путях — одно (`ivan`, в любом регистре): так пишут образцы в тестах.
PATTERNS = {
    "номер замечания (RV)": re.compile(r"\b" + "R" + "V-"),
    "номер замечания вида R-n.n-n": re.compile(r"\b" + "R" + r"-\d+(?:\.\d+)*-\d+"),
    "номер замечания вида Д-n": re.compile("(?<![А-ЯЁа-яёA-Za-z])" + "Д" + r"-\d+"),
    "папка заданий": re.compile(r"\b" + "brief" + r"s\b", re.IGNORECASE),
    "номер замечания вида IMP-n": re.compile(r"\b" + "IM" + r"P-\d+"),
    "служебное слово (роль)": re.compile("оркестр" + "атор", re.IGNORECASE),
    "служебное слово (оценка)": re.compile(r"\b" + "жю" + r"ри\b", re.IGNORECASE),
    "служебное слово (проверка)": re.compile("(?<![а-яё])(?:рев" + "ью|ревь" + "юер)", re.IGNORECASE),
    "служебное слово (рабочая группа)": re.compile("(?<![а-яё])шт" + "аб", re.IGNORECASE),
    "служебное слово (этап М)": re.compile(r"(?<!\w)" + "М" + r"\d(?!\w)"),
    "служебное слово (метка прогона)": re.compile(r"\b" + "LI" + r"VE\b"),
    "имя рабочей копии или профиля (accN)": re.compile(r"(?<![A-Za-z0-9_])" + "ac" + r"c\d"),
    "личный путь Windows": re.compile(r"[A-Za-z]:[\\/]+" + "Users" + r"\b(?![\\/]+ivan\b)", re.IGNORECASE),
    "личный путь macOS": re.compile("/" + "Users" + r"/(?!ivan\b)", re.IGNORECASE),
    "личный путь Linux": re.compile("/" + "home" + r"/(?![Ii]van\b)\w"),
    "личная почта": re.compile(
        "@(?:g"
        + "mail|yand"
        + "ex|outl"
        + "ook|hotm"
        + "ail|icl"
        + "oud|proton(?:mail)?)"
        + r"\.(?:com|ru|me)"
        "|@" + "mail" + r"\.ru(?![\w.])",
        re.IGNORECASE,
    ),
    "номер пакета работ": re.compile(
        r"\b" + "пакет" + r"\w*\s+\d+\.\d+(?!\d)(?!\.\d)(?!\s*(?:[гмкт]б|gb|mb|мин|смс|sms|₽|руб))",
        re.IGNORECASE,
    ),
}
# Исключения: путь файла от корня -> какие ключи FORBIDDEN и PATTERNS в нём допустимы.
# В LICENSE и NOTICE имя автора уместно (развилка Р-1).
EXCEPTIONS: dict[str, set[str]] = {
    "LICENSE": {"имя автора"},
    "NOTICE": {"имя автора"},
    "pyproject.toml": {"имя автора"},  # адрес репозитория в [project.urls] и в User-Agent
    # страница ведётся отдельно от этой чистки: слово там заменят, и тогда исключение убрать
    "docs/requirements.md": {"служебное слово (проверка)"},
    # тест скиллов сам ищет личные папки (macOS и Linux) в текстах скиллов: шаблоны поиска лежат в нём
    "tests/test_skills.py": {"личный путь macOS", "личный путь Linux"},
}

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


def _walk() -> list[Path]:
    return [
        p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    ]


def _git_toplevel() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out).resolve() if out else None


def candidate_files() -> list[Path]:
    # Архив, распакованный внутри чужого репозитория: git покажет файлы родителя — обходим папку.
    if _git_toplevel() != ROOT.resolve():
        return _walk()
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return _walk()
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def violations(rel: str, original: str) -> list[str]:
    """Служебное в файле: ключи правил и хеши имён; имя файла (`rel`, от корня) проверяется тоже."""
    allowed = EXCEPTIONS.get(rel, set())
    text = SERVICE_ADDRESS_RE.sub("", original)
    lowered, lowered_rel = text.lower(), rel.lower()
    found = []
    for label, needle in FORBIDDEN.items():
        if label not in allowed and (needle.lower() in lowered or needle.lower() in lowered_rel):
            found.append(label)
    if "имя участника" not in allowed:
        found += [f"имя участника ({h})" for h in known_names(text + " " + rel)]
    for label, pattern in PATTERNS.items():
        if label not in allowed and (pattern.search(text) or pattern.search(rel)):
            found.append(label)
    return found


def sponsor_in_fixture(rel: str, original: str) -> bool:
    """Название компании-спонсора в страницах-образцах (в тексте или в имени файла)."""
    return rel.startswith("tests/fixtures/") and (SPONSOR in original.lower() or SPONSOR in rel.lower())


def test_forbidden_strings_assembled_from_parts_are_not_in_this_file():
    text = Path(__file__).read_text(encoding="utf-8")
    for needle in FORBIDDEN.values():
        assert needle.lower() not in text.lower()
    assert SPONSOR not in text.lower()
    for label, pattern in PATTERNS.items():
        assert not pattern.search(text), label


# Образцы служебного и личного: по одному на каждое правило, собраны из частей.
FORBIDDEN_SAMPLES = {
    "рабочая папка команды": "см. strateg" + "-" + "hq/docs",
    "граф знаний": "карта из graph" + "ify-out",
    "имя автора": "github.com/Flos" + "Vacui/репозиторий",
    "рабочая копия задания": "папка strateg" + "-wt" + "/7.5",
    "профиль Claude": "~/.claude" + "-acc" + "2/projects",
}
PATTERN_SAMPLES = {
    "номер замечания (RV)": "см. " + "RV" + "-2.3-1",
    "номер замечания вида R-n.n-n": "замечание " + "R" + "-2.3-14",
    "номер замечания вида Д-n": "(" + "Д" + "-5)",
    "папка заданий": "docs/" + "brief" + "s/2.3.md",
    "номер пакета работ": "появится в " + "пакет" + "е 3.1",
    "номер замечания вида IMP-n": "см. " + "IM" + "P-027",
    "служебное слово (роль)": "решает " + "оркестр" + "атор",
    "служебное слово (оценка)": "видно " + "жю" + "ри",
    "служебное слово (проверка)": "по " + "рев" + "ью",
    "служебное слово (рабочая группа)": "решение " + "шт" + "аба",
    "служебное слово (этап М)": "этап " + "М" + "1",
    "служебное слово (метка прогона)": "режим " + "LI" + "VE",
    "имя рабочей копии или профиля (accN)": "профиль " + "ac" + "c1",
    "личный путь Windows": "C:" + chr(92) + "Users" + chr(92) + "anna",
    "личный путь macOS": "/" + "Users" + "/anna/work",
    "личный путь Linux": "/" + "home" + "/anna/work",
    "личная почта": "anna@" + "g" + "mail.com",
}
EXTRA_SAMPLES = (
    "R" + "-12-3",
    "(" + "Д" + "-1)",
    "в " + "пакет" + "ах 4.9 и 4.13",
    "hq-" + "ac" + "c3",
    "М" + "0" + "-" + "М" + "1",
    "М" + "1" + "-прогон",
    "D:" + chr(92) + "Users" + chr(92) + "anna",
    "C:" + chr(92) * 2 + "Users" + chr(92) * 2 + "anna",
    "C:/" + "Users" + "/anna",
    "/mnt/c/" + "Users" + "/anna",
    "в " + "шт" + "або",
)
HARMLESS = (
    "SERV-1",
    "OBSERV-2",
    "R-значение",
    "R-12",
    "версия 2.3-1",
    "ДУ-2, ДВ-4, ОМ-4, ТР-5.1, ОМ-5.3, СД-4.5",
    "ADR-0011",
    "нет брифов",
    "пакет pydantic 2.14",
    "пакет 1.5 ГБ",
    "пакет 3.5 мин",
    "пакет 2.14.3",
    "пересмотр, проверка и критик",
    "масшт" + "аб и мас" + "шт" + "абный",
    "/Users/ivan/Documents",
    "/Users/Ivan/Documents",
    "/home/ivan/work",
    "C:" + chr(92) + "Users" + chr(92) + "Ivan" + chr(92) + "проект",
    "C:" + chr(92) * 2 + "Users" + chr(92) * 2 + "Ivan" + chr(92) * 2 + "проект",
    "%USERPROFILE%" + chr(92) + "проект",
    chr(92) * 2 + "pc" + chr(92) + "Users" + chr(92) + "common",
    "ivan@example.test",
    "petrov@mail.ru.png",
    "жюрить",
    "пре" + "вью страницы",
    "М" + "1" + "2",
    "m1 и M1, chip M1 Mac",
    "sk_live_" + "abc",
    "live = []",
    "access1, account2, hacc3",
    SERVICE_ADDRESS,
    "коммит от " + SERVICE_ADDRESS + ".",
)


def test_every_pattern_has_a_sample_and_catches_it():
    assert set(PATTERN_SAMPLES) == set(PATTERNS)
    for label, sample in PATTERN_SAMPLES.items():
        assert PATTERNS[label].search(sample), label
        assert label in violations("docs/x.md", sample), label
    for sample in EXTRA_SAMPLES:
        assert violations("docs/x.md", sample), sample


def test_every_forbidden_rule_has_a_sample_and_catches_it():
    assert set(FORBIDDEN_SAMPLES) == set(FORBIDDEN)
    for label, sample in FORBIDDEN_SAMPLES.items():
        assert label in violations("docs/x.md", sample), label
        assert label in violations("docs/" + FORBIDDEN[label] + ".md", "текст"), label
        assert label not in violations("docs/x.md", "текст без служебного"), label


def test_harmless_text_is_not_caught():
    for harmless in HARMLESS:
        assert violations("docs/x.md", harmless) == [], harmless
    # логин автора вне служебного адреса по-прежнему нарушение, а в LICENSE и NOTICE допустим
    login = FORBIDDEN["имя автора"]
    assert violations("docs/x.md", f"{SERVICE_ADDRESS}, github.com/{login}/x") == ["имя автора"]
    assert violations("LICENSE", f"Copyright {login}") == []


def test_exceptions_name_existing_rules():
    for rel, labels in EXCEPTIONS.items():
        assert labels <= set(FORBIDDEN) | set(PATTERNS), rel


def test_known_names_are_found_by_hash_without_naming_them():
    word = "sample" + "token"
    hashes = {hashlib.sha256(word.encode()).hexdigest()}
    assert known_names("автор SampleToken", hashes) == [next(iter(hashes))[:8]]
    assert known_names("github.com/SAMPLETOKEN/repo", hashes)
    assert known_names("sample-token, sampletokens, mysampletoken", hashes) == []
    assert known_names("обычный текст без имён", hashes) == []
    assert known_names("sampletoken", set()) == []


def test_two_participant_names_are_kept_as_hashes():
    # Самих имён в тесте нет: потеря хеша (или пустой список) не должна пройти незамеченной.
    assert len(NAME_HASHES) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in NAME_HASHES)
    assert violations("docs/x.md", "текст") == []


def test_sponsor_company_is_found_only_in_fixtures():
    name = "Мега" + "Фон"
    assert sponsor_in_fixture("tests/fixtures/pages/cards.html", f"© {name}, 2026")
    assert sponsor_in_fixture(f"tests/fixtures/pages/{name.lower()}.html", "страница")
    assert not sponsor_in_fixture("tests/fixtures/pages/cards.html", "© Компания «Пример»")
    assert not sponsor_in_fixture("evals/cases/x/card.yaml", f"страница {name}")


def test_there_are_files_to_check():
    assert len(candidate_files()) > 10


def test_no_internal_traces_in_product():
    found = []
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        original = path.read_bytes().decode("utf-8", "ignore")
        found += [f"{rel}: {label}" for label in violations(rel, original)]
    assert not found, "служебное в продукте:\n" + "\n".join(found)


def test_no_sponsor_company_in_fixtures():
    # Страницы-образцы — вымышленные; название компании-спонсора в них не нужно.
    found = []
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        if sponsor_in_fixture(rel, path.read_bytes().decode("utf-8", "ignore")):
            found.append(rel)
    assert not found, "компания-спонсор в фикстурах:" + chr(10) + chr(10).join(found)
