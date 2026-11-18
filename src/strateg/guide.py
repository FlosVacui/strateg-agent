"""Проверка гайда интервью по рубрике `knowledge/rubrics/interview-guide.yaml` (`strateg guide check`).

Гайд — Markdown по шаблону `knowledge/templates/interview-guide.md`. Вопросы берутся из всех разделов
«…вопросы…» (и пунктов со знаком «?» в прочих): разделы «Чего не спрашиваем» и «Что слушаем» вопросами не
считаются, примеры того, чего спрашивать нельзя, лежат там.

Часть А рубрики — признаки, которые видно по тексту, без модели: вопрос о будущем, наводящий, закрытый
«да» или «нет» без продолжения, питч продукта, мнение вместо случая, число вопросов. Разбор консервативный:
явный признак называется ошибкой, сомнительный — предупреждением, и предупреждение код выхода не
меняет. Часть Б — вопросы для критика или агента: смысл вопроса код не оценивает.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from strateg.project import CliError, UsageError
from strateg.rubric_checks import knowledge_dir

RUBRIC_FILE = "interview-guide.yaml"
MAX_BYTES = 1_000_000
ERROR, WARNING = "ошибка", "предупреждение"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBERED = re.compile(r"^ {0,3}(\d{1,2})[.)]\s+(\S.*)$")
_BULLET = re.compile(r"^[-*+]\s+(\S.*)$")
_FOLLOW = re.compile(r"^(?: {2,}|\t)\s*(?:[-*+]\s+)?(\S.*)$")
_ITEM_BULLET = re.compile(r"^\s*[-*+]\s+\S")
_CHECKS = (
    "question_count",
    "future",
    "leading",
    "closed",
    "pitch",
    "opinion",
    "past_anchor",
    "sections",
    "double",
)


@dataclass
class Question:
    """Пункт раздела «Вопросы»: номер в гайде, текст и продолжение («если да — …») без номера."""

    number: int
    text: str
    follow_up: list[str] = field(default_factory=list)

    @property
    def has_follow_up(self) -> bool:
        """Продолжение — подпункт или второе предложение после первого вопросительного знака."""
        _, _, tail = self.text.partition("?")
        if re.match(r"\W*да\s*(?:/|или)\s*нет\W*$", tail.casefold()):
            tail = ""  # «Да или нет» — усиление закрытого вопроса, а не продолжение
        return bool(self.follow_up) or len(re.findall(r"\w+", tail)) >= 3

    @property
    def full_text(self) -> str:
        return " ".join([self.text, *self.follow_up])


@dataclass
class Finding:
    check: str
    id: str
    level: str
    reason: str
    question: int | None = None
    excerpt: str = ""

    def data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "check": self.check,
            "level": self.level,
            "question": self.question,
            "reason": self.reason,
            "excerpt": self.excerpt,
        }

    def line(self) -> str:
        where = f"вопрос {self.question}" if self.question is not None else "гайд"
        quote = f" («{self.excerpt}»)" if self.excerpt else ""
        return f"{where}: {self.level} {self.id}: {self.reason}{quote}"


@dataclass
class Guide:
    questions: list[Question]
    headings: list[str]
    has_questions_section: bool


@dataclass
class Report:
    guide: Guide
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]


# ---------------------------------------------------------------------------
# Рубрика
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    check: str
    rule: str
    reason: str
    patterns: tuple[re.Pattern[str], ...]
    weak: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class GuideRubric:
    min_questions: int
    max_questions: int
    sections: tuple[str, ...]
    open_words: re.Pattern[str]
    continue_words: re.Pattern[str]
    rules: dict[str, Rule]
    part_b: tuple[dict[str, str], ...]


def _compile(items: Any, where: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for item in items or []:
        try:
            out.append(re.compile(str(item)))
        except re.error as exc:
            raise CliError(
                f"рубрика {RUBRIC_FILE}: выражение «{item}» в {where} не разбирается: {exc}"
            ) from exc
    return tuple(out)


def load_rubric(path: Path | None = None) -> GuideRubric:
    path = path or knowledge_dir() / "rubrics" / RUBRIC_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CliError(f"рубрика гайда {RUBRIC_FILE} не читается: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("part_a"), list):
        raise CliError(f"рубрика {RUBRIC_FILE}: нет части А (part_a)")
    rules: dict[str, Rule] = {}
    for item in raw["part_a"]:
        check = item.get("check")
        if check not in _CHECKS or check in rules:
            raise CliError(f"рубрика {RUBRIC_FILE}: проверка «{check}» неизвестна или повторена")
        rules[check] = Rule(
            id=str(item["id"]),
            check=check,
            rule=str(item["rule"]),
            reason=str(item.get("reason", "")),
            patterns=_compile(item.get("patterns"), f"{item['id']}, patterns"),
            weak=_compile(item.get("weak"), f"{item['id']}, weak"),
        )
    missing = [c for c in _CHECKS if c not in rules]
    if missing:
        raise CliError(f"рубрика {RUBRIC_FILE}: нет проверок {', '.join(missing)}")
    part_b = tuple(
        {"id": str(q["id"]), "question": str(q["question"]), "no_means": str(q["no_means"])}
        for q in raw.get("part_b", [])
    )
    return GuideRubric(
        min_questions=int(raw["min_questions"]),
        max_questions=int(raw["max_questions"]),
        sections=tuple(str(s) for s in raw["sections"]),
        open_words=re.compile(str(raw["open_words"])),
        continue_words=re.compile(str(raw["continue_words"])),
        rules=rules,
        part_b=part_b,
    )


# ---------------------------------------------------------------------------
# Разбор гайда
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """Текст для сверки с выражениями рубрики: нижний регистр, «ё» как «е», пробелы схлопнуты."""
    return " ".join(text.casefold().replace("ё", "е").split())


def _numbered(lines: list[str]) -> list[Question]:
    out: list[Question] = []
    for line in lines:
        m = _NUMBERED.match(line)
        if m:
            out.append(Question(int(m.group(1)), m.group(2).strip()))
        elif out and line.strip():
            f = _FOLLOW.match(line)
            if f:
                if _ITEM_BULLET.match(line):
                    out[-1].follow_up.append(f.group(1).strip())
                else:  # перенос длинного вопроса на следующую строку
                    out[-1].text += " " + f.group(1).strip()
    return out


def _bulleted(lines: list[str]) -> list[Question]:
    out: list[Question] = []
    for line in lines:
        m = _BULLET.match(line)
        if m:
            out.append(Question(len(out) + 1, m.group(1).strip()))
        elif out and line.strip():
            f = _FOLLOW.match(line)
            if f:
                out[-1].follow_up.append(f.group(1).strip())
    return out


_SKIPPED = ("чего не спрашиваем", "что слушаем")


def _is_questions_title(title: str) -> bool:
    """«Вопросы…», «Уточняющие вопросы»: раздел с вопросами; «Какие вопросы не задаём» — нет."""
    t = fold(title)
    return "вопрос" in t and not re.search(r"\b(?:не|нельзя|плох|запрещ)\w*", t)


def parse_guide(text: str) -> Guide:
    """Вопросы гайда: нумерованные пункты всех разделов «…вопросы…» и пункты с «?» в прочих разделах.

    Разделы «Чего не спрашиваем» и «Что слушаем» вопросами не считаются. Нет раздела с вопросами —
    нумерованные пункты всего файла, кроме этих двух.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heads: list[tuple[int, str, int]] = []  # (уровень, заголовок, индекс строки)
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else _HEADING.match(line)
        if m:
            heads.append((len(m.group(1)), m.group(2), i))
    has_section = any(_is_questions_title(h[1]) for h in heads)
    blocks: list[tuple[list[str], str]] = []  # (строки раздела, вид)
    first = heads[0][2] if heads else len(lines)
    if first:
        blocks.append((lines[:first], "other"))
    chain: list[tuple[int, str]] = []
    for k, (level, title, idx) in enumerate(heads):
        while chain and chain[-1][0] >= level:
            chain.pop()
        chain.append((level, title))
        end = heads[k + 1][2] if k + 1 < len(heads) else len(lines)
        if any(fold(t).startswith(_SKIPPED) for _, t in chain):
            kind = "skipped"
        elif any(_is_questions_title(t) for _, t in chain):
            kind = "questions"
        else:
            kind = "other"
        blocks.append((lines[idx + 1 : end], kind))
    questions: list[Question] = []
    for body, kind in blocks:
        if kind == "skipped":
            continue
        if kind == "questions":
            found = _numbered(body) or _bulleted(body)
        else:
            found = _numbered(body)
            if has_section:  # вне разделов с вопросами считаются только пункты со знаком «?»
                found = [q for q in found if "?" in q.full_text]
        questions.extend(found)
    used: set[int] = set()
    for q in questions:  # номера второго раздела не повторяют номера первого
        if q.number in used:
            q.number = max(used) + 1
        used.add(q.number)
    return Guide(questions=questions, headings=[h[1] for h in heads], has_questions_section=has_section)


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


def first_hit(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return " ".join(m.group(0).split())[:40]
    return None


def _opens(rubric: GuideRubric, text: str, words: int) -> bool:
    """Среди первых слов вопроса есть вопросительное слово или просьба рассказать: вопрос открытый."""
    head = " ".join(text.split()[:words])
    return bool(rubric.open_words.search(head))


def check_question(q: Question, rubric: GuideRubric) -> list[Finding]:
    """Находки по одному вопросу: явный признак — ошибка, сомнительный — предупреждение."""
    out: list[Finding] = []
    text, whole = fold(q.text), fold(q.full_text)
    for check in ("future", "leading", "pitch"):
        rule = rubric.rules[check]
        hit = first_hit(rule.patterns, whole)
        if hit:
            out.append(Finding(check, rule.id, ERROR, rule.reason, q.number, hit))
            continue
        hit = first_hit(rule.weak, whole)
        if hit:
            out.append(Finding(check, rule.id, WARNING, rule.reason + " (проверьте)", q.number, hit))
    closed = rubric.rules["closed"]
    if not q.has_follow_up and not rubric.continue_words.search(text):
        hit = first_hit(closed.patterns, text)
        if hit:
            out.append(Finding("closed", closed.id, ERROR, closed.reason, q.number, hit))
        elif not _opens(rubric, text, 4):
            hit = first_hit(closed.weak, text)
            if hit:
                out.append(
                    Finding("closed", closed.id, WARNING, closed.reason + " (проверьте)", q.number, hit)
                )
    opinion = rubric.rules["opinion"]
    hit = first_hit(opinion.weak, whole)
    if hit:
        out.append(Finding("opinion", opinion.id, WARNING, opinion.reason, q.number, hit))
    double = rubric.rules["double"]
    if q.text.count("?") >= 3 and not q.follow_up:
        out.append(Finding("double", double.id, WARNING, double.reason, q.number))
    return out


def check_guide(text: str, rubric: GuideRubric | None = None) -> Report:
    rubric = rubric or load_rubric()
    guide = parse_guide(text)
    findings: list[Finding] = []
    rule = rubric.rules["question_count"]
    n = len(guide.questions)
    if n < rubric.min_questions or n > rubric.max_questions:
        findings.append(
            Finding(
                "question_count",
                rule.id,
                ERROR,
                f"вопросов {n}, нужно от {rubric.min_questions} до {rubric.max_questions}",
            )
        )
    for q in guide.questions:
        findings.extend(check_question(q, rubric))
    past = rubric.rules["past_anchor"]
    if guide.questions and not any(first_hit(past.patterns, fold(q.full_text)) for q in guide.questions):
        findings.append(Finding("past_anchor", past.id, WARNING, past.reason))
    sect = rubric.rules["sections"]
    have = [fold(h) for h in guide.headings]
    for name in rubric.sections:
        if not any(h.startswith(fold(name)) for h in have):
            findings.append(Finding("sections", sect.id, WARNING, f"{sect.reason} «{name}»"))
    return Report(guide=guide, findings=findings)


# ---------------------------------------------------------------------------
# strateg guide check
# ---------------------------------------------------------------------------


def _read(raw: str) -> tuple[Path, str]:
    path = Path(raw)
    try:
        if not path.is_file():
            raise CliError(f"файл {raw} не найден: гайд лежит в outputs/ проекта или в .tmp/")
        if path.stat().st_size > MAX_BYTES:
            raise CliError(f"файл {raw} больше {MAX_BYTES // 1000} КБ: это не гайд")
        return path, path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliError(f"файл {raw} не в UTF-8: сохраните гайд как UTF-8") from exc
    except OSError as exc:
        raise CliError(f"файл {raw} не читается: {exc.strerror or exc}") from exc


def cmd_guide_check(args: argparse.Namespace) -> int:
    if not getattr(args, "file", None):
        raise UsageError("укажите файл гайда: `strateg guide check <файл>`")
    path, text = _read(args.file)
    rubric = load_rubric()
    report = check_guide(text, rubric)
    if not report.guide.questions:
        raise CliError(
            "в гайде не нашлось ни одного вопроса: вопросы идут нумерованным списком в разделе «Вопросы…» "
            "(шаблон — knowledge/templates/interview-guide.md)"
        )
    errors, warnings = report.errors, report.warnings
    semantic = [dict(q) for q in rubric.part_b]
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": not errors,
                    "file": path.name,
                    "questions": len(report.guide.questions),
                    "errors": [f.data() for f in errors],
                    "warnings": [f.data() for f in warnings],
                    "semantic": semantic,
                },
                ensure_ascii=False,
            )
        )
    else:
        n = len(report.guide.questions)
        print(f"Гайд {path.name}: вопросов {n}; ошибок {len(errors)}, предупреждений {len(warnings)}.")
        for f in [*errors, *warnings]:
            print("  " + f.line())
        if not report.guide.has_questions_section:
            print("  раздела «Вопросы» нет: вопросами названы все нумерованные пункты файла")
        print("Смысловая проверка (критик или агент: «да» или «нет» и номер вопроса):")
        for q in semantic:
            print(f"  {q['id']}: {q['question']}")
        print("Ошибок нет: часть А пройдена." if not errors else "Часть А не пройдена: исправьте ошибки.")
    return 1 if errors else 0
