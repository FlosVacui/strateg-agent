"""Таблица из текста источника: разбор, типы значений, единицы, периоды (только стандартная библиотека).

Текст таблицы (`strateg add` для `.csv` и `.xlsx`) — строки вида `A1: значение | B1: значение` под меткой
`[лист: имя]`. Пустые ячейки в тексте не пишутся, поэтому пропуск — это ячейка, которой нет в строке.
Модуль берут `strateg data check` и помощник расчётов (`strateg_calc`), поэтому тяжёлых зависимостей нет.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from strateg import units

SHEET = re.compile(r"^\[лист: (.*)\]$")
_CELL = re.compile(r"(?:^|\s\|\s)([A-Z]{1,3})([1-9][0-9]*): ")
_SPACES = "    "

MONTHS = {
    "январь": 1, "января": 1, "янв": 1, "февраль": 2, "февраля": 2, "фев": 2, "март": 3, "марта": 3,
    "мар": 3, "апрель": 4, "апреля": 4, "апр": 4, "май": 5, "мая": 5, "июнь": 6, "июня": 6, "июн": 6,
    "июль": 7, "июля": 7, "июл": 7, "август": 8, "августа": 8, "авг": 8, "сентябрь": 9, "сентября": 9,
    "сен": 9, "сент": 9, "октябрь": 10, "октября": 10, "окт": 10, "ноябрь": 11, "ноября": 11, "ноя": 11,
    "декабрь": 12, "декабря": 12, "дек": 12,
}  # fmt: skip


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def col_letters(index: int) -> str:
    out = ""
    while index:
        index, r = divmod(index - 1, 26)
        out = chr(65 + r) + out
    return out


@dataclass
class Sheet:
    name: str
    cells: dict[int, dict[int, str]] = field(default_factory=dict)  # строка -> столбец -> значение


def parse_sheets(text: str) -> list[Sheet]:
    sheets: list[Sheet] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        m = SHEET.match(line.strip())
        if m:
            sheets.append(Sheet(m.group(1)))
            continue
        if not sheets or not line.strip():
            continue
        found = list(_CELL.finditer(line))
        for i, m in enumerate(found):
            end = found[i + 1].start() if i + 1 < len(found) else len(line)
            sheets[-1].cells.setdefault(int(m.group(2)), {})[col_index(m.group(1))] = line[
                m.end() : end
            ].strip()
    return sheets


# --- значения -----------------------------------------------------------------------------------------

_NUM = (
    r"[-+−]?\d{1,3}(?:,\d{3})+\.\d+|[-+−]?\d{1,3}(?:,\d{3}){2,}"  # 1,234.50 и 1,234,567 — американский вид
    r"|[-+−]?\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?|[-+−]?\d+(?:[.,]\d+)?"
)
_VALUE = re.compile(rf"^\s*(?P<pre>[^\d\s+\-−]{{0,6}})?\s*(?P<num>{_NUM})\s*(?P<post>.*?)\s*$", re.IGNORECASE)
_units_in = units.units_in


def unit_label(scale: str | None, measure: str | None) -> str | None:
    return " ".join(x for x in (scale, measure) if x) or None


def parse_number(value: str) -> tuple[float, str | None] | None:
    """(число, единица) или None. Единица — «тыс. ₽», «%», … ; None — единицы в значении нет.

    «(1 234)» — отрицательное число, «1,234.50» — американская запись.
    """
    v = value.strip()
    negative = len(v) > 2 and v.startswith("(") and v.endswith(")")
    m = _VALUE.match(v[1:-1] if negative else v)
    if not m:
        return None
    pre, num, post = m.group("pre") or "", m.group("num"), m.group("post")
    if re.match(r"^\d{4}-\d{2}", v) or re.match(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$", v):
        return None
    rest = f"{pre} {post}".strip()
    scale = measure = None
    if rest:
        scale, measure = _units_in(rest)
        if scale is None and measure is None:
            return None
    raw = num.replace("−", "-")
    if raw.count(",") > 1 or ("," in raw and "." in raw):
        raw = raw.replace(",", "")
    for ch in _SPACES:
        raw = raw.replace(ch, "")
    try:
        number = float(raw.replace(",", "."))
    except ValueError:
        return None
    return (-number if negative else number), unit_label(scale, measure)


def header_unit(header: str) -> tuple[str | None, str | None]:
    """Единица из заголовка: «Выручка, тыс. ₽», «Время (мин)», «Доля %»."""
    tail = re.split(r",|\(|\[|/", header, maxsplit=1)
    candidate = header if len(tail) == 1 else header.split(tail[0], 1)[1]
    scale, measure = _units_in(candidate)
    if scale is None and measure is None and len(tail) == 1:
        return None, None
    return scale, measure


@dataclass(frozen=True)
class Period:
    year: int
    month: int | None = None
    day: int | None = None
    quarter: int | None = None

    @property
    def kind(self) -> str:
        if self.quarter is not None:
            return "квартал"
        if self.day is not None:
            return "день"
        if self.month is not None:
            return "месяц"
        return "год"

    def ordinal(self) -> int:
        if self.quarter is not None:
            return self.year * 4 + self.quarter - 1
        if self.day is not None:
            return dt.date(self.year, self.month or 1, self.day).toordinal()
        if self.month is not None:
            return self.year * 12 + self.month - 1
        return self.year

    def label(self) -> str:
        if self.quarter is not None:
            return f"{self.year} Q{self.quarter}"
        if self.day is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.month is not None:
            return f"{self.year:04d}-{self.month:02d}"
        return str(self.year)


def _valid_date(y: int, m: int, d: int) -> bool:
    try:
        dt.date(y, m, d)
    except ValueError:
        return False
    return True


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
_QUARTER_WORD = r"(?:кв\.?|квартал)"


def _parse_period(value: str, year_ok: bool = False) -> tuple[Period, str] | None:
    """(период, вид записи): «гггг-мм-дд», «дд.мм.гггг», «д/м/гггг», «д месяц гггг», «квартал» и т. д."""
    v = value.strip()
    if not v:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", v)
    if m and _valid_date(int(m[1]), int(m[2]), int(m[3])):
        return Period(int(m[1]), int(m[2]), int(m[3])), "гггг-мм-дд"
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", v)
    if m and _valid_date(int(m[3]), int(m[2]), int(m[1])):
        return Period(int(m[3]), int(m[2]), int(m[1])), "дд.мм.гггг"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m and _valid_date(int(m[3]), int(m[2]), int(m[1])):
        return Period(int(m[3]), int(m[2]), int(m[1])), "д/м/гггг"
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m and 1 <= int(m[2]) <= 12:
        return Period(int(m[1]), int(m[2])), "гггг-мм"
    m = re.fullmatch(r"(\d{2})\.(\d{4})", v)
    if m and 1 <= int(m[1]) <= 12:
        return Period(int(m[2]), int(m[1])), "мм.гггг"
    m = re.fullmatch(
        rf"(?:Q([1-4])[ -]?(\d{{4}})|(\d{{4}})[ -]?Q([1-4])|(IV|III|II|I|[1-4])(?:-?й)?\s*{_QUARTER_WORD}\s*"
        r"(\d{4})(?:\s*г\.?)?)",
        v,
        re.I,
    )
    if m:
        q = m[1] or m[4] or m[5]
        y = m[2] or m[3] or m[6]
        return Period(int(y), quarter=_ROMAN.get(q.upper()) or int(q)), "квартал"
    m = re.fullmatch(r"(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4})(?:\s*г\.?)?", v)
    if m and m[2].casefold() in MONTHS and _valid_date(int(m[3]), MONTHS[m[2].casefold()], int(m[1])):
        return Period(int(m[3]), MONTHS[m[2].casefold()], int(m[1])), "д месяц гггг"
    m = re.fullmatch(r"([^\W\d_]+)\.?\s+(\d{4})(?:\s*г\.?)?", v)
    if m and m[1].casefold() in MONTHS:
        return Period(int(m[2]), MONTHS[m[1].casefold()]), "месяц гггг"
    if year_ok and re.fullmatch(r"\d{4}", v) and 1900 <= int(v) <= 2100:
        return Period(int(v)), "год"
    return None


def parse_period(value: str, year_ok: bool = False) -> Period | None:
    found = _parse_period(value, year_ok)
    return found[0] if found else None


_YEAR_HEADER = re.compile(r"\b(год|year|г\.)\b", re.IGNORECASE)
_LEADING_ZERO = re.compile(r"0\d+")


@dataclass
class Column:
    letter: str
    name: str
    values: list[str | None]  # по строкам данных; None — ячейки нет или в ней заглушка («н/д», «-»)
    kind: str = "текст"
    missing: int = 0
    stubs: int = 0  # из пропусков — записанные словами или прочерком
    unit: str | None = None
    unit_mixed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    period: dict[str, Any] | None = None

    @property
    def present(self) -> list[tuple[int, str]]:
        return [(i, v) for i, v in enumerate(self.values) if v is not None]


@dataclass
class Table:
    sheet: str
    header_row: int
    has_header: bool
    rows: list[int]  # номера строк листа, по которым идут значения
    columns: list[Column]
    empty_rows: list[int]
    duplicates: list[list[int]]
    title_rows: list[int] = field(default_factory=list)  # название над таблицей
    notes: list[str] = field(default_factory=list)  # предупреждения о таблице в целом (заголовок)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name or c.letter == name:
                return c
        raise KeyError(name)

    def records(self) -> list[dict[str, str | None]]:
        return [{c.name: c.values[i] for c in self.columns} for i in range(len(self.rows))]


def _is_year(v: str) -> bool:
    return re.fullmatch(r"\d{4}", v.strip()) is not None and 1900 <= int(v) <= 2100


def _data_like(v: str) -> bool:
    return not _is_year(v) and (parse_number(v) is not None or parse_period(v) is not None)


def _row_is_header(cells: dict[int, str]) -> bool:
    """Шапка: меньше половины значений похожи на данные (годы в шапке — не данные)."""
    vals = [cells[c] for c in sorted(cells)]
    if sum(1 for v in vals if _data_like(v)) * 2 < len(vals):
        return True
    rest = vals[1:]  # «Показатель; янв 2025; фев 2025»: текст, а дальше периоды
    return len(rest) >= 2 and not _data_like(vals[0]) and all(_is_year(v) or parse_period(v) for v in rest)


def _is_unit_row(cells: dict[int, str]) -> bool:
    """Строка под шапкой, где в каждой ячейке только единица: «тыс. ₽», «%», «шт.»."""
    return bool(cells) and all(
        len(v) <= 16 and not _data_like(v) and units.units_in(v) != (None, None) for v in cells.values()
    )


def _locate_header(sheet: Sheet, width: int) -> tuple[list[int], int]:
    """Строки-названия над таблицей и строка, с которой начинается шапка (или первые данные)."""
    rows = sorted(sheet.cells)
    idx, titles = 0, []
    while idx < len(rows) - 1 and len(titles) < 3:
        cur, nxt = sheet.cells[rows[idx]], sheet.cells[rows[idx + 1]]
        if len(cur) * 2 < width and len(nxt) > len(cur):  # название или объединённая ячейка над шапкой
            titles.append(rows[idx])
            idx += 1
        else:
            break
    return titles, rows[idx]


def build_table(sheet: Sheet) -> Table | None:
    if not sheet.cells:
        return None
    last = max(sheet.cells)
    width = max(max(r) for r in sheet.cells.values())
    cols = [c for c in range(1, width + 1) if any(c in r for r in sheet.cells.values())]
    titles, first = _locate_header(sheet, len(cols))
    head = sheet.cells[first]
    has_header = _row_is_header(head)
    unit_row = None
    if has_header:
        after = [r for r in sorted(sheet.cells) if r > first][:1]
        if after and _is_unit_row(sheet.cells[after[0]]):
            unit_row = after[0]
    start = (unit_row or first) + 1 if has_header else first
    rows = list(range(start, last + 1))
    empty = [r for r in rows if r not in sheet.cells]
    used = [r for r in rows if r in sheet.cells]
    notes: list[str] = []
    columns: list[Column] = []
    unnamed: list[str] = []
    for c in cols:
        letter = col_letters(c)
        name = head.get(c, "").strip() if has_header else ""
        if has_header and unit_row and c in sheet.cells[unit_row]:
            name = f"{name or letter}, {sheet.cells[unit_row][c]}"
        if not name:
            name = letter
            unnamed.append(letter)
        raw = [sheet.cells[r].get(c) for r in used]
        stubs = sum(1 for v in raw if v is not None and units.is_missing_token(v))
        values = [None if v is None or not v.strip() or units.is_missing_token(v) else v for v in raw]
        columns.append(Column(letter, name, values, stubs=stubs))
    if not has_header:
        notes.append("заголовок не определён: первая строка похожа на данные, столбцы названы буквами")
    elif unnamed:
        notes.append(f"в заголовке нет имён у столбцов {', '.join(unnamed)}: названы буквами")
    seen: dict[tuple[str | None, ...], list[int]] = {}
    for r in used:
        seen.setdefault(tuple(sheet.cells[r].get(c) for c in cols), []).append(r)
    dups = [rs for rs in seen.values() if len(rs) > 1]
    table = Table(
        sheet.name, first if has_header else 0, has_header, used, columns, empty, dups, titles, notes
    )
    for col in columns:
        _profile_column(col, has_header)
    return table


def _profile_column(col: Column, has_header: bool) -> None:
    rows = col.values
    col.missing = sum(1 for v in rows if v is None)
    present = [v for v in rows if v is not None]
    if not present:
        col.kind = "пусто"
        return
    nums = [parse_number(v) for v in present]
    year_ok = bool(_YEAR_HEADER.search(col.name))
    pers = [_parse_period(v, year_ok) for v in present]
    n_num = sum(1 for x in nums if x is not None)
    n_per = sum(1 for x in pers if x is not None)
    codes = [v for v in present if _LEADING_ZERO.fullmatch(v.strip())]
    bad_dates = [v for v, p in zip(present, pers, strict=True) if p is None]
    if n_per * 5 >= len(present) * 4:
        col.kind = "дата"
        if bad_dates:
            col.warnings.append(
                f"в столбце дат {len(bad_dates)} значений не даты (например «{bad_dates[0]}»)"
            )
        found = [p for p in pers if p is not None]
        main_kind = Counter(q.kind for q, _ in found).most_common(1)[0][0]
        formats = Counter(fmt for q, fmt in found if q.kind == main_kind)
        if len(formats) > 1:
            col.warnings.append(
                "смешанные форматы дат: " + ", ".join(f"{f} ({n})" for f, n in sorted(formats.items()))
            )
        _profile_period(col, [p for p, _ in found])
    elif n_num * 5 >= len(present) * 4 and codes:
        col.kind = "текст"
        col.warnings.append(
            f"ведущие нули (например «{codes[0]}»): это код, а не число, столбец считается текстом"
        )
    elif n_num * 5 >= len(present) * 4:
        col.kind = "число"
        bad = [v for v, x in zip(present, nums, strict=True) if x is None]
        if bad:
            col.warnings.append(f"в числовом столбце {len(bad)} значений не числа (например «{bad[0]}»)")
        _profile_units(col, [x for x in nums if x is not None])
    else:
        col.kind = "текст"
        if n_num * 2 >= len(present):
            col.warnings.append("половина значений похожа на числа, но столбец не числовой: проверьте формат")
        if n_per * 10 >= len(present) * 3 and n_per >= 2:
            col.warnings.append(
                f"смешанные форматы дат: распознано {n_per} из {len(present)}, не распознано "
                f"{len(bad_dates)} (например «{bad_dates[0]}»); столбец считается текстом"
            )


def _profile_units(col: Column, nums: list[tuple[float, str | None]]) -> None:
    hs, hm = header_unit(col.name) if col.name else (None, None)
    head = unit_label(hs, hm)
    used = Counter(u for _, u in nums if u)
    bare = sum(1 for _, u in nums if not u)
    col.unit = head or (next(iter(used)) if len(used) == 1 else None)
    if len(used) > 1:
        col.unit_mixed = sorted(used)
        col.warnings.append("смешение единиц в столбце: " + ", ".join(sorted(used)))
    elif used and head and next(iter(used)) != head:
        col.unit_mixed = sorted({head, next(iter(used))})
        col.warnings.append(
            f"единица в заголовке «{head}», а в значениях «{next(iter(used))}»: смешение единиц"
        )
    if used and bare and not head:
        col.warnings.append(f"у части значений есть единица ({next(iter(used))}), у части нет ({bare})")


def _profile_period(col: Column, periods: list[Period]) -> None:
    kinds = Counter(p.kind for p in periods)
    kind = kinds.most_common(1)[0][0]
    if len(kinds) > 1:
        col.warnings.append("разный вид периодов в столбце: " + ", ".join(sorted(kinds)))
    same = [p for p in periods if p.kind == kind]
    uniq = sorted({p.ordinal(): p for p in same}.items())
    step = kind
    if kind == "день":
        ords = [o for o, _ in uniq]
        if all(p.day == 1 for _, p in uniq) or all(
            (dt.date.fromordinal(o) + dt.timedelta(days=1)).day == 1 for o in ords
        ):
            step = "месяц"
            uniq = sorted({(p.year * 12 + p.month - 1): p for _, p in uniq}.items())  # type: ignore[operator]
        elif len(ords) > 1:
            diffs = [b - a for a, b in zip(ords, ords[1:], strict=False)]
            med = statistics.median(diffs)
            if med == 7 and sum(1 for d in diffs if d == 7) * 2 >= len(diffs):
                step = "неделя"
            elif med > 1.5 and sum(1 for d in diffs if d == 1) * 5 < len(diffs) * 3:
                step = "нерегулярный"
    dup_count = len(same) - len({p.ordinal() for p in same})
    gaps: list[str] = []
    if step in ("день", "месяц", "квартал", "год") and len(uniq) > 1:
        have = {o for o, _ in uniq}
        lo, hi = uniq[0][0], uniq[-1][0]
        for o in range(lo, hi + 1):
            if o not in have:
                gaps.append(_ordinal_label(step, o))
    elif step == "неделя":
        have = {o for o, _ in uniq}
        lo, hi = uniq[0][0], uniq[-1][0]
        gaps = [dt.date.fromordinal(o).isoformat() for o in range(lo, hi + 1, 7) if o not in have]
    shown_step = "день" if step in ("неделя", "нерегулярный") else step
    first = _ordinal_label(shown_step, uniq[0][0]) if uniq else None
    last = _ordinal_label(shown_step, uniq[-1][0]) if uniq else None
    col.period = {
        "from": first,
        "to": last,
        "step": step,
        "gaps": gaps,
        "duplicates": dup_count,
        "distinct": len(uniq),
    }
    if gaps:
        shown = ", ".join(gaps[:5]) + (f" и ещё {len(gaps) - 5}" if len(gaps) > 5 else "")
        col.warnings.append(f"дыры в ряду ({step}): нет {shown}")
    if dup_count:
        col.warnings.append(
            f"повторы периодов: {dup_count} (если на период несколько строк по разрезам, это нормально)"
        )
    if step == "нерегулярный":
        col.warnings.append("шаг ряда нерегулярный: дыры не определить")


def _ordinal_label(step: str, o: int) -> str:
    if step == "день":
        return dt.date.fromordinal(o).isoformat()
    if step == "месяц":
        return f"{o // 12:04d}-{o % 12 + 1:02d}"
    if step == "квартал":
        return f"{o // 4} Q{o % 4 + 1}"
    return str(o)


def tables_of(text: str) -> list[Table]:
    out = []
    for sheet in parse_sheets(text):
        t = build_table(sheet)
        if t is not None:
            out.append(t)
    return out
