"""Профиль таблицы: `strateg data check <И-n | файл>` (ADR-0004).

Читает текст таблицы источника (обезличенную копию, не исходник) и печатает по столбцам тип, пропуски,
единицы, периоды и дубликаты строк. Сомнительное — предупреждение, код выхода остаётся 0. Проект не
меняется, кроме одной записи в журнал. Минимальный объём: регрессий, сезонности и статистических
тестов здесь нет.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from strateg import materials, tables
from strateg import project as proj
from strateg.commands import emit
from strateg.models import Source
from strateg.project import CliError, Project

MAX_SHOWN = 10


def table_source(p: Project, raw: str) -> Source:
    """Источник-таблица по номеру `И-n` или по файлу в `inputs/`."""
    raw = raw.strip()
    if raw.startswith("И-"):
        found = next((s for s in p.sources.sources if s.id == raw), None)
        if found is None:
            raise CliError(f"источника {raw} нет в проекте")
    else:
        file: Path | None = None
        try:
            file = materials.resolve_material(p, raw)
            rel = materials.inputs_relpath(p, file)
        except CliError:
            rel = f"inputs/{Path(raw).name}"
        found = next((s for s in p.sources.sources if s.path == rel), None)
        if found is None:
            raise CliError(f"файл {raw} не зарегистрирован: сначала `strateg add {Path(raw).name}`")
        if (
            file is not None
            and found.material_sha256
            and not found.anonymized
            and proj.sha256_bytes(file.read_bytes()) != found.material_sha256
        ):
            raise CliError(
                f"файл {raw} не тот, что записан как {found.id}: содержимое отличается; "
                f"`strateg data check {found.id}` читает записанный текст"
            )
    if found.kind != "таблица":
        raise CliError(f"{found.id}: это {found.kind}, а не таблица (.csv или .xlsx)")
    if (found.anonymized and not found.anonymization_confirmed) or (
        found.closed_markers and not found.closed_confirmed
    ):
        raise CliError(
            f"{found.id}: человек ещё не подтвердил замены или открытость материала: "
            f"`strateg add --confirm {found.id}`"
        )
    if not found.text_path:
        raise CliError(f"{found.id}: у источника нет текста таблицы")
    return found


def load_text(p: Project, src: Source) -> str:
    path = p.root / (src.text_path or "")
    if not path.is_file():
        raise CliError(f"{src.id}: файл текста {src.text_path} не найден")
    return path.read_bytes().decode("utf-8")


def _missing_note(c: tables.Column, n: int) -> str:
    words = f" (в том числе записано словом или прочерком: {c.stubs})" if c.stubs else ""
    return f"пропусков: {c.missing} из {n}{words}"


def profile(text: str) -> list[dict[str, Any]]:
    out = []
    for t in tables.tables_of(text):
        n = len(t.rows)
        cols = []
        for c in t.columns:
            cols.append(
                {
                    "column": c.letter,
                    "name": c.name,
                    "type": c.kind,
                    "missing": c.missing,
                    "missing_share": round(c.missing / n, 4) if n else 0.0,
                    "missing_as_text": c.stubs,
                    "unit": c.unit,
                    "units_mixed": c.unit_mixed,
                    "period": c.period,
                    "warnings": list(c.warnings)
                    + ([_missing_note(c, n)] if c.missing and c.kind != "пусто" else [])
                    + (["столбец пуст"] if c.kind == "пусто" else []),
                }
            )
        warnings = list(t.notes) + [f"{c['name']}: {w}" for c in cols for w in c["warnings"]]
        if t.empty_rows:
            warnings.append(f"пустых строк внутри таблицы: {len(t.empty_rows)}")
        if t.duplicates:
            extra = sum(len(g) - 1 for g in t.duplicates)
            warnings.append(
                f"дубликаты строк: {extra} лишних (строки {', '.join(str(g[0]) for g in t.duplicates[:5])} "
                "и их повторы)"
            )
        out.append(
            {
                "sheet": t.sheet,
                "header": t.has_header,
                "header_row": t.header_row or None,
                "title_rows": t.title_rows,
                "rows": n,
                "columns": cols,
                "empty_rows": len(t.empty_rows),
                "duplicates": [{"rows": g} for g in t.duplicates],
                "warnings": warnings,
            }
        )
    return out


def _lines(sid: str, sheets: list[dict[str, Any]]) -> list[str]:
    lines = []
    for s in sheets:
        if not s["header"]:
            head = "заголовка нет, столбцы по буквам"
        elif s["title_rows"]:
            head = f"заголовок в строке {s['header_row']}, над ним названий: {len(s['title_rows'])}"
        else:
            head = "заголовок в первой строке"
        lines.append(
            f"{sid}, лист «{s['sheet']}»: строк данных {s['rows']}, столбцов {len(s['columns'])}; {head}."
        )
        for c in s["columns"]:
            parts = [c["type"]]
            if c["unit"]:
                parts.append(f"единица {c['unit']}")
            if c["missing"]:
                parts.append(f"пропусков {c['missing']} ({c['missing_share'] * 100:.0f} %)")
            pr = c["period"]
            if pr:
                parts.append(f"период {pr['from']}…{pr['to']}, шаг {pr['step']}")
            lines.append(f"  {c['column']} «{c['name']}»: " + ", ".join(parts))
        if s["warnings"]:
            lines.append("  Предупреждения:")
            lines += [f"    - {w}" for w in s["warnings"][: MAX_SHOWN * 3]]
        else:
            lines.append("  Замечаний нет.")
    return lines


def cmd_data_check(args: argparse.Namespace) -> int:
    from strateg.commands import _open

    p = _open(args)
    started = time.monotonic()
    src = table_source(p, args.target)
    text = load_text(p, src)
    sheets = profile(text)
    if not sheets:
        raise CliError(f"{src.id}: в тексте нет таблицы (листов с данными)")
    with proj.transaction(p, rollback=False):
        p.add_event(
            p.event(
                kind="действие",
                name="strateg data check",
                idempotency_hash=proj.key_hash("data check", src.id, src.text_sha256, len(p.journal)),
                status="выполнено",
                duration_s=round(time.monotonic() - started, 2),
                message=f"{src.id}: предупреждений {sum(len(s['warnings']) for s in sheets)}",
            )
        )
        p.save_state()
    return emit(
        args,
        {"source": src.id, "sheets": sheets, "warnings": [w for s in sheets for w in s["warnings"]]},
        _lines(src.id, sheets),
    )
