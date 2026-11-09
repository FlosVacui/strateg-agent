"""Порог условия отказа и значение показателя: число, знак, единица (ADR-0016).

Порог в записи решения — свободный текст с числом: «больше 15 %», «ниже 33 %», «меньше 0 ₽». Разбор
консервативный: понятен только вид «<знак> <число> [<единица> [пояснение без цифр]]». Всё остальное
(«не ниже 20 %», «около 15», «ниже 20 % или выше 40 %», «больше 20 обращений на 1000») не разбирается:
`strateg due` в этом случае просит сравнить вручную, а не гадает.

Единицы две — процент и рубль; значение и порог в разных единицах не сравниваются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Op = Literal[">", "<"]

# Слова знака. «Не ниже», «не больше» и подобное не разбираются: что считать срабатыванием, неясно.
_OPS: dict[str, Op] = {
    "больше": ">",
    "выше": ">",
    "более": ">",
    ">": ">",
    "меньше": "<",
    "ниже": "<",
    "менее": "<",
    "<": "<",
}
_MINUS = "-−–"
_NUMBER = (
    rf"[{_MINUS}]?\d{{1,3}}(?:[   ]\d{{3}})+(?:[.,]\d+)?"  # 15 000 или 15 000,5
    rf"|[{_MINUS}]?\d+(?:[.,]\d+)?"
)
_UNIT = r"(?P<unit>%|процент\w*|₽|руб\w*|р(?!\w)\.?)"
_THRESHOLD = re.compile(
    rf"^(?P<op>{'|'.join(re.escape(o) for o in _OPS)})\s*(?P<num>{_NUMBER})\s*(?:{_UNIT})?\s*(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_VALUE = re.compile(rf"^\s*(?P<num>{_NUMBER})\s*(?:{_UNIT})?\.?\s*$", re.IGNORECASE)
# Слова, из-за которых порог — не одно условие: «ниже 20 % или выше 40 %».
_SPLITTERS = frozenset({"или", "либо", "и", "а", "но", "если", "когда"})
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Threshold:
    op: Op
    number: float
    unit: str | None  # «%», «₽» или None


def _number(text: str) -> float:
    cleaned = re.sub(r"[   ]", "", text).replace(",", ".")
    return float(re.sub(f"^[{_MINUS}]", "-", cleaned))


def _unit(text: str | None) -> str | None:
    if text is None:
        return None
    return "%" if text.startswith(("%", "п", "П")) else "₽"


def parse_threshold(text: str) -> Threshold | None:
    """Знак, число и единица порога; None — порог не разобран."""
    m = _THRESHOLD.match(text.strip().rstrip(".;").casefold())
    if m is None:
        return None
    unit = _unit(m["unit"])
    tail = m["tail"].strip()
    if tail:
        if unit is None or re.search(r"\d", tail):
            return None  # после числа без единицы идёт чужая единица («обращений на 1000») или второе число
        if any(w in _SPLITTERS for w in _WORD.findall(tail)):
            return None
    return Threshold(_OPS[m["op"]], _number(m["num"]), unit)


def parse_value(text: str) -> tuple[float, str | None] | None:
    """Число и единица значения показателя («18», «18 %», «1 500 ₽»); None — не число."""
    m = _VALUE.match(text.casefold())
    if m is None:
        return None
    return _number(m["num"]), _unit(m["unit"])


def fires(threshold: Threshold, value: float) -> bool:
    """Значение пересекло порог: строго больше или строго меньше."""
    return value > threshold.number if threshold.op == ">" else value < threshold.number


def show_number(x: float) -> str:
    """15.0 -> «15», 12.5 -> «12,5», 15000 -> «15 000»."""
    text = f"{x:,.10g}".replace(",", " ").replace(".", ",") if abs(x) < 1e15 else str(x)
    return text.replace("-", "−") if x < 0 else text


def show_amount(value: float, unit: str | None) -> str:
    return f"{show_number(value)} {unit}" if unit else show_number(value)
