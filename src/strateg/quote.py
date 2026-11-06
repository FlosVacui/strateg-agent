"""Проверка цитаты по тексту снимка (ADR-0005).

Способы по порядку: точное вхождение, вид без пробелов и дефисов, нечёткое совпадение
(`rapidfuzz.fuzz.partial_ratio_alignment`, порог из моделей, только для цитаты от 40 знаков).
Нормализация и первые два способа — из `strateg.models`.

Одного порога мало: подмена цифры и вставка «не» почти не меняют сходство. Поэтому:
- число в цитате не должно быть частью другого числа в снимке («99 рублей» не находится в «199 рублей»,
  «990» — в «1 990»); это касается и точного вхождения;
- в найденном окне нечёткого совпадения числа и отрицания совпадают с цитатой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from strateg.models import (
    FUZZY_THRESHOLD,
    MIN_FUZZY_QUOTE_LEN,
    has_meaning,
    normalize_quote,
    strip_spaces_and_hyphens,
)

NEGATIONS = frozenset(
    {
        "не",
        "нет",
        "ни",
        "нельзя",
        "без",
        "никогда",
        "никак",
        "нигде",
        "никто",
        "ничего",
        "ничто",
        "not",
        "no",
        "never",
        "without",
        "cannot",
    }
)
_NUMBER = re.compile(r"\d+(?: \d{3}(?!\d))*(?:[.,]\d+)?")
_WORD = re.compile(r"\w+")
_BEFORE_QUOTE = " \t\n\"'(«,:;-"  # знаки между отрицанием и началом цитаты
PAGE_MARK = re.compile(r"^\[стр\. (\d+)\]$", re.MULTILINE)


@dataclass(frozen=True)
class QuoteMatch:
    method: str  # «точное вхождение», «без пробелов и дефисов» или «нечёткое»
    similarity: float


def extend_numbers(s: str, a: int, b: int) -> tuple[int, int]:
    """Расширяет окно [a, b) до границ режущих его чисел: «99» в «199», «990» в «1 990», «5» в «5,5»."""
    n = len(s)
    moved = True
    while moved:
        moved = False
        if a < b and s[a].isdigit() and a > 0:
            if s[a - 1].isdigit():
                a -= 1
                moved = True
            elif s[a - 1] in ",." and a > 1 and s[a - 2].isdigit():
                a -= 2
                moved = True
            elif (
                s[a - 1] == " "
                and a > 1
                and s[a - 2].isdigit()
                and re.match(r"\d{3}(?!\d)", s[a : a + 4])
                and re.search(r"(?<!\d)\d{1,3} $", s[:a])
            ):
                a -= 2
                moved = True
        if a < b and s[b - 1].isdigit() and b < n:
            if s[b].isdigit():
                b += 1
                moved = True
            elif s[b] in ",." and b + 1 < n and s[b + 1].isdigit():
                b += 2
                moved = True
            elif (
                s[b] == " "
                and re.match(r" \d{3}(?!\d)", s[b : b + 5])
                and re.search(r"(?<!\d)\d{1,3}$", s[a:b])
            ):
                b += 4
                moved = True
    return a, b


def numbers(text: str) -> list[str]:
    """Числа текста в одном виде: без пробелов-разрядов, запятая как точка; отсортированный список."""
    return sorted(m.group().replace(" ", "").replace(",", ".") for m in _NUMBER.finditer(text))


def negation_count(text: str) -> int:
    return sum(1 for w in _WORD.findall(text) if w in NEGATIONS)


def cuts_negation(s: str, a: int) -> bool:
    """Перед местом `a` в тексте стоит отрицание: цитата, начатая после него, говорит обратное."""
    before = s[:a].rstrip(_BEFORE_QUOTE)
    m = re.search(r"(\w+)$", before)
    return bool(m) and m.group(1) in NEGATIONS and (a == 0 or not s[a - 1].isalnum())


def _find_whole(s: str, q: str) -> bool:
    """Есть ли `q` в `s` так, чтобы граница не резала число и не отрезала отрицание."""
    start = 0
    while True:
        i = s.find(q, start)
        if i < 0:
            return False
        if extend_numbers(s, i, i + len(q)) == (i, i + len(q)) and not cuts_negation(s, i):
            return True
        start = i + 1


def _token_edges(s: str, a: int, b: int) -> tuple[int, int]:
    """Окно до границ слов: окно нечёткого поиска может резать слово «не» или число."""
    while a > 0 and not s[a - 1].isspace():
        a -= 1
    while b < len(s) and not s[b].isspace():
        b += 1
    return a, b


def _window_agrees(q: str, s: str, a: int, b: int) -> bool:
    """В окне числа и отрицания те же, что в цитате."""
    a, b = extend_numbers(s, a, b)
    a, b = _token_edges(s, a, b)
    window = s[a:b]
    return (
        numbers(window) == numbers(q)
        and negation_count(window) == negation_count(q)
        and not cuts_negation(s, a)
    )


def fuzzy_allowed(quote: str) -> bool:
    """Нечёткая проверка — только для цитаты от 40 знаков (как требует модель)."""
    return len(" ".join(quote.split())) >= MIN_FUZZY_QUOTE_LEN


def check_quote(quote: str, snapshot: str) -> QuoteMatch | None:
    """Проверяет цитату по тексту снимка; None — цитата не найдена."""
    if not has_meaning(quote):
        return None
    q = normalize_quote(quote)
    s = normalize_quote(snapshot)
    if _find_whole(s, q):
        return QuoteMatch("точное вхождение", 100.0)
    if q in s:  # дословно есть, но граница режет число или отрицание: другими способами не обойти
        return None
    if _find_whole(strip_spaces_and_hyphens(s), strip_spaces_and_hyphens(q)):
        return QuoteMatch("без пробелов и дефисов", 100.0)
    if not fuzzy_allowed(quote) or not s:
        return None
    al = fuzz.partial_ratio_alignment(q, s)
    if al is None or al.score < FUZZY_THRESHOLD:
        return None
    if not _window_agrees(q, s, al.dest_start, al.dest_end):
        return None
    return QuoteMatch("нечёткое", round(al.score, 2))


def split_pages(text: str) -> list[tuple[int, str]]:
    """Текст PDF-снимка по страницам: (номер страницы, текст). Без меток страниц — пустой список."""
    marks = list(PAGE_MARK.finditer(text))
    pages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        pages.append((int(m.group(1)), text[m.end() : end]))
    return pages


def find_page(quote: str, text: str) -> int | None:
    """Номер страницы PDF, на которой целиком находится цитата; None — на одной странице не найдена."""
    best: tuple[float, int] | None = None
    for number, page in split_pages(text):
        found = check_quote(quote, page)
        if found and (best is None or found.similarity > best[0]):
            best = (found.similarity, number)
    return best[1] if best else None


def explain_miss(quote: str, snapshot: str) -> str:
    """Почему цитата не нашлась: короткое пояснение для отказа."""
    if not has_meaning(quote):
        return "в цитате нет ни буквы, ни цифры"
    q = normalize_quote(quote)
    s = normalize_quote(snapshot)
    if q in s:
        if any(cuts_negation(s, i) for i in (m.start() for m in re.finditer(re.escape(q), s))):
            return "перед цитатой в снимке стоит отрицание («не», «нет», «без»): включите его в цитату"
        return "число в цитате оказывается частью другого числа в снимке (например, «99» внутри «199»)"
    if strip_spaces_and_hyphens(q) in strip_spaces_and_hyphens(s):
        return "число в цитате оказывается частью другого числа в снимке (например, «99» внутри «199»)"
    if not fuzzy_allowed(quote):
        return (
            f"точного вхождения нет, а цитата короче {MIN_FUZZY_QUOTE_LEN} знаков проверяется "
            "только точным вхождением: скопируйте её из снимка дословно"
        )
    al = fuzz.partial_ratio_alignment(q, s) if s else None
    if al is not None and al.score >= FUZZY_THRESHOLD:
        return "похожее место в снимке есть, но числа или отрицания там другие: цитата искажает текст"
    best = f"{al.score:.0f}" if al is not None else "0"
    return f"такого текста в снимке нет (наибольшее сходство {best} %, порог {FUZZY_THRESHOLD} %)"
