"""Единицы и заглушки пропусков: один набор шаблонов для `tables.py` и `calc.py`.

Слова ищутся по границе слова, символы (`%`, `₽`, `$`, `€`) — где угодно: «Чистая прибыль» не «ч», «Процесс»
не «%», «Европа» не «€». Только стандартная библиотека.
"""

from __future__ import annotations

import re

# (имя, шаблон, степень десяти)
SCALES = (
    ("трлн", r"трлн\.?|триллион\w*", 12),
    ("млрд", r"млрд\.?|миллиард\w*", 9),
    ("млн", r"млн\.?|миллион\w*", 6),
    ("тыс.", r"тыс\.?|тысяч\w*", 3),
)
# (имя, символы, слова)
MEASURES = (
    ("₽", r"₽", r"руб(?:лей|ля|ль|л)?\.?|р\.?"),
    ("$", r"\$", r"usd|долл(?:аров|ара|\.)?"),
    ("€", r"€", r"eur|евро"),
    ("%", r"%", r"процент\w*|проц\.|п\.\s?п\.?"),
    ("шт.", None, r"шт\.?|штук\w*"),
    ("мин", None, r"мин\.?|минут\w*"),
    ("ч", None, r"ч\.?|час(?:а|ов|ы)?"),
    ("сек", None, r"сек\.?|секунд\w*|с\."),
    ("ТБ", None, r"тб|tb"),
    ("ГБ", None, r"гб|gb"),
    ("МБ", None, r"мб|mb"),
)
# значение ячейки, которое означает «данных нет»
MISSING_TOKENS = frozenset(
    {"н/д", "нд", "н.д.", "нет данных", "n/a", "na", "n.a.", "-", "—", "–", "пусто", "null", "none"}
)


def _word(pattern: str) -> str:
    return rf"(?<!\w)(?:{pattern})(?!\w)"


def is_missing_token(value: str) -> bool:
    return value.strip().casefold() in MISSING_TOKENS


def units_in(text: str) -> tuple[str | None, str | None]:
    """Множитель и мера в обрывке текста (заголовок или хвост значения); None — не нашлось."""
    low = text.casefold().strip(" ()[],;.")
    scale = measure = None
    for name, pat, _ in SCALES:
        if re.search(_word(pat), low):
            scale = name
            break
    for name, symbols, words in MEASURES:
        if (symbols and re.search(symbols, low)) or re.search(_word(words), low):
            measure = name
            break
    return scale, measure


_SCALE_AFTER = [(re.compile(rf"^\s*(?:{pat})(?!\w)", re.IGNORECASE), power) for _, pat, power in SCALES]
_PERCENT_AFTER = re.compile(r"^\s*(?:%|процент\w*|проц\.|п\.\s?п\.?)", re.IGNORECASE)


def scale_after(tail: str) -> int | None:
    """Степень десяти, если хвост после числа начинается с «тыс.», «млн», «миллиона»…"""
    for rx, power in _SCALE_AFTER:
        if rx.match(tail):
            return power
    return None


def percent_after(tail: str) -> bool:
    return bool(_PERCENT_AFTER.match(tail))
