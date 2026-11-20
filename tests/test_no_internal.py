"""Тест-страж: в продукте нет служебного штаба, графа знаний, личных путей и имени автора,
номеров замечаний и заданий, ссылок на рабочую папку заданий, служебных слов штаба, имён
профилей Claude и рабочих копий, личных адресов почты и имён участников, а также названия
компании-спонсора в фикстурах.

Имена участников в тесте не записаны: хранятся хеши sha256 слов, а тест хеширует слова файлов.
Строки поиска собраны из частей, чтобы тест не находил сам себя.
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
    "штаб команды": "strateg" + "-" + "hq",
    "граф знаний": "graph" + "ify",
    "личный путь (\\)": "C:" + "\\" + "Users",
    "личный путь (/)": "C:" + "/" + "Users",
    "имя автора": "Flos" + "Vacui",
    "рабочая копия штаба": "strateg" + "-wt",
    "профиль Claude": ".claude" + "-acc",
}
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


# Номера замечаний и заданий: шаблоны, тоже собранные из частей.
PATTERNS = {
    "номер замечания (RV)": re.compile(r"\b" + "R" + "V-"),
    "номер замечания вида R-n.n-n": re.compile(r"\bR-\d+\.\d+-\d+"),
    "папка заданий": re.compile(r"\b" + "brief" + r"s\b", re.IGNORECASE),
    "номер замечания вида IMP-n": re.compile(r"\b" + "IM" + r"P-\d+"),
    "служебное слово (роль штаба)": re.compile("оркестр" + "атор", re.IGNORECASE),
    "служебное слово (оценка)": re.compile(r"\b" + "жю" + r"ри\b", re.IGNORECASE),
    "служебное слово (проверка штаба)": re.compile("(?<![а-яё])(?:рев" + "ью|ревь" + "юер)", re.IGNORECASE),
    "служебное слово (этап М)": re.compile(r"(?<![\w-])" + "М" + r"\d(?![\w-])"),
    "служебное слово (метка прогона)": re.compile(r"\b" + "LI" + r"VE\b"),
    "личный путь macOS": re.compile("/" + "Users" + r"/(?!ivan\b)"),
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
# Исключения: путь файла от корня -> какие ключи FORBIDDEN в нём допустимы.
# В LICENSE и NOTICE имя автора уместно (развилка Р-1).
EXCEPTIONS: dict[str, set[str]] = {
    "LICENSE": {"имя автора"},
    "NOTICE": {"имя автора"},
    "pyproject.toml": {"имя автора"},  # адрес репозитория в [project.urls] и в User-Agent
    # страница ведётся отдельно от этой чистки: слово там заменят, и тогда исключение убрать
    "docs/requirements.md": {"служебное слово (проверка штаба)"},
    "tests/test_skills.py": {"личный путь macOS"},  # шаблон поиска личных путей в скиллах
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


def test_forbidden_strings_assembled_from_parts_are_not_in_this_file():
    text = Path(__file__).read_text(encoding="utf-8")
    for needle in FORBIDDEN.values():
        assert needle.lower() not in text.lower()
    for label, pattern in PATTERNS.items():
        assert not pattern.search(text), label


def test_patterns_catch_service_numbers_and_words():
    samples = {
        "номер замечания (RV)": "см. " + "RV" + "-2.3-1",
        "номер замечания вида R-n.n-n": "замечание " + "R" + "-2.3-14",
        "папка заданий": "docs/" + "brief" + "s/2.3.md",
        "номер пакета работ": "появится в " + "пакет" + "е 3.1",
        "номер замечания вида IMP-n": "см. " + "IM" + "P-027",
        "служебное слово (роль штаба)": "решает " + "оркестр" + "атор",
        "служебное слово (оценка)": "видно " + "жю" + "ри",
        "служебное слово (проверка штаба)": "по " + "рев" + "ью",
        "личный путь macOS": "/" + "Users" + "/anna/work",
        "личная почта": "anna@" + "g" + "mail.com",
        "служебное слово (этап М)": "этап " + "М" + "1",
        "служебное слово (метка прогона)": "режим " + "LI" + "VE",
    }
    for label, sample in samples.items():
        assert PATTERNS[label].search(sample), label
    for harmless in (
        "SERV-1",
        "OBSERV-2",
        "R-значение",
        "версия 2.3-1",
        "нет брифов",
        "пакет pydantic 2.14",
        "пакет 1.5 ГБ",
        "пакет 3.5 мин",
        "пакет 2.14.3",
        "пересмотр, проверка и критик",
        "/Users/ivan/Documents",
        "ivan@example.test",
        "petrov@mail.ru.png",
        "жюрить",
        "пре" + "вью страницы",
        "М" + "1" + "2",
        "m1 и M1",
        "sk_live_" + "abc",
        "live = []",
    ):
        assert not any(p.search(harmless) for p in PATTERNS.values()), harmless


def test_known_names_are_found_by_hash_without_naming_them():
    word = "sample" + "token"
    hashes = {hashlib.sha256(word.encode()).hexdigest()}
    assert known_names("автор SampleToken", hashes) == [next(iter(hashes))[:8]]
    assert known_names("github.com/SAMPLETOKEN/repo", hashes)
    assert known_names("sample-token, sampletokens, mysampletoken", hashes) == []
    assert known_names("обычный текст без имён", hashes) == []
    assert known_names("sampletoken", set()) == []


def test_there_are_files_to_check():
    assert len(candidate_files()) > 10


def test_no_internal_traces_in_product():
    found = []
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_bytes().decode("utf-8", "ignore").lower()
        for label, needle in FORBIDDEN.items():
            if label in EXCEPTIONS.get(rel, set()):
                continue
            if needle.lower() in text or needle.lower() in rel.lower():
                found.append(f"{rel}: {label}")
        original = path.read_bytes().decode("utf-8", "ignore")
        if "имя участника" not in EXCEPTIONS.get(rel, set()):
            found += [f"{rel}: имя участника ({h})" for h in known_names(original + " " + rel)]
        for label, pattern in PATTERNS.items():
            if label in EXCEPTIONS.get(rel, set()):
                continue
            if pattern.search(original) or pattern.search(rel):
                found.append(f"{rel}: {label}")
    assert not found, "служебное в продукте:\n" + "\n".join(found)


def test_no_sponsor_company_in_fixtures():
    # Страницы-образцы — вымышленные; название компании-спонсора в них не нужно.
    name = ("мега" + "фон").lower()
    found = []
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        if not rel.startswith("tests/fixtures/"):
            continue
        text = path.read_bytes().decode("utf-8", "ignore").lower()
        if name in text or name in rel.lower():
            found.append(rel)
    assert not found, "компания-спонсор в фикстурах:" + chr(10) + chr(10).join(found)
