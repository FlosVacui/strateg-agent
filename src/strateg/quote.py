"""Проверка цитаты по тексту снимка (ADR-0005).

Способы по порядку: точное вхождение, вид без пробелов и дефисов, нечёткое совпадение
(`rapidfuzz.fuzz.partial_ratio_alignment`, порог из моделей, только для цитаты от 40 знаков).
Нормализация и вид «без пробелов и дефисов» — из `strateg.models`.

Одного порога мало: подмена цифры, вставка «не» или приставка «бес-» почти не меняют сходство.
Поэтому для любого способа:
- цитата начинается и кончается на границе слова: «платно» не находится в «бесплатно»;
- число в цитате не может быть частью другого числа: «99» не находится в «199», в «−99», в «200-300»,
  в «x99» и в «1 990»; цифры не склеиваются через пробел и дефис («5-10» не «510»);
- до цитаты, в пределах трёх слов в том же предложении, нет отрицания («не», «нет», «без» …), а сразу
  после неё, в пределах двух слов, нет «не», «нет», «кроме»;
- знаки вроде «³» и «½» не превращаются в обычные цифры (сверка идёт до NFKC);
для нечёткого совпадения дополнительно: слова цитаты идут в тексте подряд, без вставок и пропусков, и
отличаются только опечатками внутри слова (первый и последний знак те же); числа и отрицания те же.
Лишний текст допустим только по краям найденного места.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from strateg.models import (
    FUZZY_THRESHOLD,
    MIN_FUZZY_QUOTE_LEN,
    has_meaning,
    normalize_quote,
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
# Слова справа от цитаты, которые меняют её смысл: «бесплатно, кроме корпоративных», «бесплатно не будет».
_RIGHT_STOPS = frozenset({"не", "нет", "кроме"})
_NUMBER = re.compile(r"(?:[^\W\d_]|№)*(?:(?<!\d)[-+])?\d+(?: \d{3}(?!\d))*(?:[.,]\d+)?")
_WORD = re.compile(r"\w+")
_BEFORE_SENTENCE_END = ".!?…;\n"
PAGE_MARK = re.compile(r"^\[стр\. (\d+)\]$", re.MULTILINE)
PAGE_MARK_INLINE = re.compile(r"\[стр\. \d+\]")
_PRIVATE_BASE = 0xF0000


@dataclass(frozen=True)
class QuoteMatch:
    method: str  # «точное вхождение», «без пробелов и дефисов» или «нечёткое»
    similarity: float


def protect(text: str) -> str:
    """Знаки вроде «³» и «½» (категория No) NFKC превращает в обычные цифры: прячем их до нормализации."""
    return "".join(chr(_PRIVATE_BASE + ord(c)) if unicodedata.category(c) == "No" else c for c in text)


def norm_text(text: str) -> str:
    """Нормализация цитаты и снимка для сверки: ADR-0005 плюс защита знаков вроде «³»."""
    return normalize_quote(protect(text))


# ---------------------------------------------------------------------------
# Числа и слова
# ---------------------------------------------------------------------------


def numbers(text: str) -> list[str]:
    """Числа текста: со знаком, если перед знаком не цифра; без пробелов-разрядов; запятая как точка."""
    return sorted(m.group().replace(" ", "").replace(",", ".") for m in _NUMBER.finditer(text))


def word_count(text: str) -> int:
    return len(_WORD.findall(normalize_quote(text)))


def negation_count(text: str) -> int:
    return sum(1 for w in _WORD.findall(text) if w in NEGATIONS)


def extend_numbers(s: str, a: int, b: int) -> tuple[int, int]:
    """Расширяет окно [a, b) до границ режущих его чисел.

    Режут число: цифра перед цифрой, «99» в «199», «5» в «0,5», «2026» в «2025/2026», «30» в «10:30»,
    «990» в «1 990», знак «-»/«+» или «№» или буква вплотную перед числом, хвост диапазона «200-300»,
    голова диапазона «200» в «200-300».
    """
    n = len(s)
    moved = True
    while moved:
        moved = False
        if a < b and s[a].isdigit() and a > 0:
            p = s[a - 1]
            if p.isdigit():
                a -= 1
                moved = True
            elif p in ",./:" and a > 1 and s[a - 2].isdigit():
                a -= 2
                moved = True
            elif p in "-+" or p == "№" or p.isalpha():
                a -= 1
                moved = True
            elif (
                p == " "
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
            elif s[b] in ",./:" and b + 1 < n and s[b + 1].isdigit():
                b += 2
                moved = True
            elif s[b] == "-" and b + 1 < n and s[b + 1].isdigit():
                b += 1
                moved = True
            elif (
                s[b] == " "
                and re.match(r" \d{3}(?!\d)", s[b : b + 5])
                and re.search(r"(?<!\d)\d{1,3}$", s[a:b])
            ):
                b += 4
                moved = True
    return a, b


def cuts_negation(s: str, a: int, b: int) -> bool:
    """Отрицание рядом с окном [a, b): три слова слева или два слова справа, в том же предложении."""
    seg = s[:a]
    cut = max(seg.rfind(c) for c in _BEFORE_SENTENCE_END)
    if any(w in NEGATIONS for w in _WORD.findall(seg[cut + 1 :])[-3:]):
        return True
    if b > 0 and s[b - 1] in _BEFORE_SENTENCE_END:  # цитата кончается знаком конца предложения
        return False
    tail = s[b : b + 200]
    ends = [i for c in _BEFORE_SENTENCE_END if (i := tail.find(c)) >= 0]
    return any(w in _RIGHT_STOPS for w in _WORD.findall(tail[: min(ends, default=len(tail))])[:2])


def _mid_word(s: str, a: int, b: int, *, hyphen_joins: bool = False) -> bool:
    """Окно [a, b) начинается или кончается внутри слова."""
    n = len(s)
    if 0 < a < n and s[a].isalnum():
        p = s[a - 1]
        if p.isalnum() or (hyphen_joins and p == "-" and a > 1 and s[a - 2].isalnum()):
            return True
    if 0 < b < n and s[b - 1].isalnum():
        q = s[b]
        if q.isalnum() or (hyphen_joins and q == "-" and b + 1 < n and s[b + 1].isalnum()):
            return True
    return False


def _problem(s: str, a: int, b: int, *, hyphen_joins: bool = False) -> str | None:
    """Почему окно [a, b) не годится как цитата: слово, число или отрицание; None — годится."""
    if _mid_word(s, a, b, hyphen_joins=hyphen_joins):
        return "слово"
    if extend_numbers(s, a, b) != (a, b):
        return "число"
    if cuts_negation(s, a, b):
        return "отрицание"
    return None


# ---------------------------------------------------------------------------
# Точное вхождение и вид без пробелов и дефисов
# ---------------------------------------------------------------------------


def _compact(text: str) -> tuple[str, list[int]]:
    """Текст без пробелов и дефисов и индексы знаков в исходном тексте.

    Между цифрами на месте убранного остаётся пробел: «5-10» и «1 1» не становятся «510» и «11».
    """
    out: list[str] = []
    idx: list[int] = []
    skipped_at: int | None = None
    for i, ch in enumerate(text):
        if ch.isspace() or ch == "-":
            if skipped_at is None:
                skipped_at = i
            continue
        if skipped_at is not None and out and out[-1].isdigit() and ch.isdigit():
            out.append(" ")
            idx.append(skipped_at)
        skipped_at = None
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def _occurrences(haystack: str, needle: str):
    start = 0
    while (i := haystack.find(needle, start)) >= 0:
        yield i
        start = i + 1


def _find_exact(s: str, q: str) -> tuple[bool, str | None]:
    """(найдено, причина отказа первого найденного места)."""
    reason = None
    for i in _occurrences(s, q):
        problem = _problem(s, i, i + len(q))
        if problem is None:
            return True, None
        reason = reason or problem
    return False, reason


def _find_compact(s: str, q: str) -> tuple[bool, str | None]:
    cs, idx = _compact(s)
    cq, _ = _compact(q)
    if not cq:
        return False, None
    reason = None
    for i in _occurrences(cs, cq):
        a, b = idx[i], idx[i + len(cq) - 1] + 1
        problem = _problem(s, a, b, hyphen_joins=True)
        if problem is None:
            return True, None
        reason = reason or problem
    return False, reason


# ---------------------------------------------------------------------------
# Нечёткое совпадение
# ---------------------------------------------------------------------------


def _typo(a: str, b: str) -> bool:
    """Слова отличаются опечаткой внутри слова: 1–2 знака, первый и последний знак те же."""
    if a == b:
        return True
    if any(c.isdigit() for c in a + b):
        return False
    short = min(len(a), len(b))
    if short < 5 or a[0] != b[0] or a[-1] != b[-1]:
        return False
    return Levenshtein.distance(a, b) <= (2 if short >= 9 else 1)


def _agrees(q: str, s: str) -> bool:
    """В тексте есть место, где слова цитаты идут подряд и отличаются разве что опечатками в словах.

    Вставок и пропусков слов внутри нет: «249 тыс. руб.» не равно «249 руб.», а «только» не появляется
    из ничего. Лишний текст допустим только по краям места. Числа и отрицания места те же, что в цитате,
    а границы места проходят проверку `_problem`.
    """
    q_words = _WORD.findall(q)
    spans = [(m.start(), m.end(), m.group()) for m in _WORD.finditer(s)]
    words = [w for _, _, w in spans]
    n = len(q_words)
    for k in range(len(words) - n + 1):
        if all(_typo(x, y) for x, y in zip(q_words, words[k : k + n], strict=True)):
            start, end = spans[k][0], spans[k + n - 1][1]
            place = s[start:end]
            if (
                _problem(s, start, end) is None
                and numbers(place) == numbers(q)
                and negation_count(place) == negation_count(q)
            ):
                return True
    return False


def fuzzy_allowed(quote: str) -> bool:
    """Нечёткая проверка — только для цитаты от 40 знаков (как требует модель)."""
    return len(" ".join(quote.split())) >= MIN_FUZZY_QUOTE_LEN


def check_quote(quote: str, snapshot: str) -> QuoteMatch | None:
    """Проверяет цитату по тексту снимка; None — цитата не найдена или искажает текст."""
    if not has_meaning(quote):
        return None
    q, s = norm_text(quote), norm_text(snapshot)
    found, _ = _find_exact(s, q)
    if found:
        return QuoteMatch("точное вхождение", 100.0)
    if q in s:  # дословно есть, но граница режет слово, число или отрицание: обходить нельзя
        return None
    found, _ = _find_compact(s, q)
    if found:
        return QuoteMatch("без пробелов и дефисов", 100.0)
    if not fuzzy_allowed(quote) or not s:
        return None
    if _compact(q)[0] in _compact(s)[0]:
        return None
    al = fuzz.partial_ratio_alignment(q, s)
    if al is None or al.score < FUZZY_THRESHOLD or not _agrees(q, s):
        return None
    return QuoteMatch("нечёткое", round(al.score, 2))


_REASONS = {
    "слово": "цитата начинается или кончается в середине слова («платно» в «бесплатно»): "
    "берите слова целиком",
    "число": "число в цитате оказывается частью другого числа или записано со знаком, диапазоном "
    "(«99» внутри «199», «−99», «200-300»)",
    "отрицание": "рядом с цитатой в снимке, в трёх словах слева или в двух справа, стоит отрицание "
    "или оговорка "
    "(«не», «нет», «без», «кроме»): включите её в цитату",
}


def explain_miss(quote: str, snapshot: str) -> str:
    """Почему цитата не нашлась: короткое пояснение для отказа."""
    if not has_meaning(quote):
        return "в цитате нет ни буквы, ни цифры"
    q, s = norm_text(quote), norm_text(snapshot)
    _, reason = _find_exact(s, q)
    if reason is None and q not in s:
        _, reason = _find_compact(s, q)
    if reason:
        return _REASONS[reason]
    if not fuzzy_allowed(quote):
        return (
            f"точного вхождения нет, а цитата короче {MIN_FUZZY_QUOTE_LEN} знаков проверяется "
            "только точным вхождением: скопируйте её из снимка дословно"
        )
    al = fuzz.partial_ratio_alignment(q, s) if s else None
    if al is not None and al.score >= FUZZY_THRESHOLD:
        return "похожее место в снимке есть, но числа, отрицания или слова там другие: цитата искажает текст"
    best = f"{al.score:.0f}" if al is not None else "0"
    return f"такого текста в снимке нет (наибольшее сходство {best} %, порог {FUZZY_THRESHOLD} %)"


# ---------------------------------------------------------------------------
# Страницы PDF
# ---------------------------------------------------------------------------


def split_pages(text: str) -> list[tuple[int, str]]:
    """Текст PDF-снимка по страницам: (номер страницы, текст). Без меток страниц — пустой список."""
    marks = list(PAGE_MARK.finditer(text))
    pages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        pages.append((int(m.group(1)), text[m.end() : end]))
    return pages


def pages_with_quote(quote: str, text: str) -> list[tuple[int, QuoteMatch]]:
    """Страницы, на каждой из которых цитата целиком находится."""
    out = []
    for number, page in split_pages(text):
        found = check_quote(quote, page)
        if found:
            out.append((number, found))
    return out


def find_page(quote: str, text: str) -> int | None:
    """Номер страницы PDF, на которой целиком находится цитата (лучшая по сходству); None — нет такой."""
    best: tuple[float, int] | None = None
    for number, found in pages_with_quote(quote, text):
        if best is None or found.similarity > best[0]:
            best = (found.similarity, number)
    return best[1] if best else None
