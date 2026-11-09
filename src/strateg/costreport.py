"""`strateg cost report [--json]`: стоимость проекта по ролям, шагам и моделям из `cost.jsonl` (ADR-0010).

Отчёт берёт числа из файла учёта, а не из памяти модели. В сумму входят те же записи, что и в стоимость
проекта (`project.cost_total`): по каждой сессии последний итог или записи по сообщениям. Точность называется
всегда: «измерено» (деньги от среды), «оценка» (токены × таблица цен `config/prices.yaml`), «нет данных».
На подписке стоимость — эквивалент по цене API, а не счёт.

В отчёте разбора: сколько записей транскриптов пропущено (битые строки, сообщения без учётных полей) — по
служебному файлу смещений `.tmp/cost/offsets.json`, и какие модели без цены. Повышения модели — из событий
«выбор модели» с именем «повышение».

Веб-поиск показан отдельной строкой в итоге и у каждой роли: число вызовов и оценка денег (плата за поиск и
запрос к модели поиска, `config/prices.yaml`); эти деньги уже входят в сумму роли.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from strateg import accounting, budget
from strateg import project as proj
from strateg.commands import _open, emit
from strateg.models import CostRecord
from strateg.project import Project
from strateg.routing import UPGRADE_EVENT

NO_STEP = "не указан"
WEB_SEARCH_NOTE = "оценка"  # веб-поиск считается по таблице цен и среднему запросу, а не по данным среды
DEFAULT_SEARCH_FEE = 0.01  # $10 за 1000 поисков: запасное значение, если таблицы цен нет
SUBSCRIPTION_NOTE = (
    "На подписке это эквивалент по цене API, а не счёт: оценка. Доля лимитов подписки здесь не видна."
)


@dataclass
class Bucket:
    """Сумма по одной группе записей (роль, шаг или модель)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    web_search_usd: float = 0.0
    usd: float = 0.0
    unpriced_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)

    def add(self, r: CostRecord, search_fee: float = DEFAULT_SEARCH_FEE) -> None:
        self.calls += 1
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cache_write_5m_tokens += r.cache_write_5m_tokens
        self.cache_write_1h_tokens += r.cache_write_1h_tokens
        self.cache_read_tokens += r.cache_read_tokens
        self.web_searches += r.web_searches
        if r.cost_usd is None:
            self.unpriced_calls += 1
        else:
            self.usd += r.cost_usd
            self.web_search_usd += web_search_usd(r, search_fee)
        self.models[r.model] = self.models.get(r.model, 0) + 1


def web_search_usd(r: CostRecord, search_fee: float = DEFAULT_SEARCH_FEE) -> float:
    """Деньги записи на веб-поиск. У записей без поля (прежние версии) — плата за поиск по числу поисков."""
    if r.web_search_usd is not None:
        return r.web_search_usd
    return r.web_searches * search_fee


def _group(records: list[CostRecord], key: Any, search_fee: float = DEFAULT_SEARCH_FEE) -> dict[str, Bucket]:
    out: dict[str, Bucket] = defaultdict(Bucket)
    for r in records:
        out[key(r)].add(r, search_fee)
    return dict(out)


def _row(name_key: str, name: str, b: Bucket, total: float) -> dict[str, Any]:
    nothing = b.calls > 0 and b.unpriced_calls == b.calls  # ни у одного вызова группы нет цены
    return {
        name_key: name,
        "models": sorted(b.models),
        "calls": b.calls,
        "input_tokens": b.input_tokens,
        "output_tokens": b.output_tokens,
        "cache_write_5m_tokens": b.cache_write_5m_tokens,
        "cache_write_1h_tokens": b.cache_write_1h_tokens,
        "cache_read_tokens": b.cache_read_tokens,
        "web_searches": b.web_searches,
        "web_search_usd": round(b.web_search_usd, 6),
        "usd": None if nothing else round(b.usd, 6),  # нет цены нигде в группе: не «0 долларов»
        "unpriced_calls": b.unpriced_calls,
        "share": None if nothing or total <= 0 else round(b.usd / total, 4),
    }


def _sorted(groups: dict[str, Bucket]) -> list[tuple[str, Bucket]]:
    return sorted(groups.items(), key=lambda kv: (-kv[1].usd, kv[0]))


def breakdown_records(records: list[CostRecord]) -> list[CostRecord]:
    """Записи для разбивки по ролям, шагам и моделям: сообщения транскриптов всех сессий.

    Итог проекта (`cost_total`) может идти от среды («измерено», `total_cost_usd` стенда): в нём ролей
    нет, поэтому разбивку дают сообщения, а у сессии без сообщений — её последний итог.
    """
    by_session: dict[str, list[CostRecord]] = defaultdict(list)
    for r in records:
        by_session[r.session_id].append(r)
    out: list[CostRecord] = []
    for recs in by_session.values():
        messages = [r for r in recs if r.entry == "по сообщению"]
        totals = [r for r in recs if r.entry == "итог сессии"]
        out += messages or totals[-1:]
    return out


def report_data(p: Project) -> dict[str, Any]:
    """Данные отчёта; считаются без записи в проект."""
    total, precision, complete = proj.cost_total(p.costs)
    counted = breakdown_records(p.costs)
    base = sum(r.cost_usd for r in counted if r.cost_usd is not None)
    sessions = {r.session_id for r in p.costs}
    unpriced: dict[str, int] = defaultdict(int)
    for r in counted:
        if r.cost_usd is None and r.entry == "по сообщению":
            unpriced[r.model] += 1
    upgrades = [
        {"time": e.time.isoformat(), "step": e.step, "role": e.role, "model": e.model, "reason": e.message}
        for e in p.journal
        if e.kind == "выбор модели" and e.name == UPGRADE_EVENT
    ]
    fee = DEFAULT_SEARCH_FEE
    try:
        prices = budget.load_prices()
        accessed: str | None = prices.accessed
        fee = prices.web_search_usd_per_1000 / 1000
    except Exception:  # noqa: BLE001 - отчёт не зависит от таблицы цен: дата не видна, остальное есть
        accessed = None
    overall = _group(counted, lambda r: "", fee).get("", Bucket())
    return {
        "project": p.id,
        "total_usd": total,
        "precision": precision,
        "complete": complete,
        "records": len(counted),
        "breakdown_usd": round(base, 6) if counted else None,
        "sessions": len(sessions),
        "prices_accessed": accessed,
        "subscription_note": SUBSCRIPTION_NOTE,
        "web_search": {
            "calls": overall.web_searches,
            "usd": round(overall.web_search_usd, 6),
            "precision": WEB_SEARCH_NOTE,
        },
        "roles": [_row("role", k, b, base) for k, b in _sorted(_group(counted, lambda r: r.role, fee))],
        "steps": [
            _row("step", k, b, base) for k, b in _sorted(_group(counted, lambda r: r.step or NO_STEP, fee))
        ],
        "models": [_row("model", k, b, base) for k, b in _sorted(_group(counted, lambda r: r.model, fee))],
        "skipped_records": accounting.skipped_total(p.root),
        "unpriced_models": [{"model": m, "calls": n} for m, n in sorted(unpriced.items())],
        "model_upgrades": upgrades,
    }


def _usd(x: float | None) -> str:
    return "нет цены" if x is None else f"${x:.4f}".replace(".", ",")


def _sum(row: dict[str, Any]) -> str:
    """Сумма группы: «нет цены», если цены нет ни у одного вызова; частичная — с числом вызовов без цены."""
    text = _usd(row["usd"])
    if row["usd"] is not None and row["unpriced_calls"]:
        text += f" (вызовов без цены: {row['unpriced_calls']})"
    return text


def _share(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f} %".replace(".", ",")


def _web_search(calls: int, usd: float) -> str:
    """«веб-поиск: 21 вызовов, $0,2461 (оценка)»: плата за поиск и запрос к модели поиска."""
    return f"веб-поиск: {calls} вызовов, {_usd(usd)} ({WEB_SEARCH_NOTE})"


def _tokens(row: dict[str, Any]) -> str:
    cache_write = row["cache_write_5m_tokens"] + row["cache_write_1h_tokens"]
    text = (
        f"ввод {row['input_tokens']}, вывод {row['output_tokens']}, "
        f"запись кэша {cache_write}, чтение кэша {row['cache_read_tokens']}"
    )
    return text + (
        ", " + _web_search(row["web_searches"], row["web_search_usd"]) if row["web_searches"] else ""
    )


def report_lines(d: dict[str, Any]) -> list[str]:
    head = (
        f"Стоимость проекта {d['project']}: {_usd(d['total_usd'])} ({d['precision']})"
        if d["total_usd"] is not None
        else f"Стоимость проекта {d['project']}: нет данных"
    )
    lines = [head + ("" if d["complete"] or d["total_usd"] is None else "; часть записей без цены")]
    lines.append(f"Записей: {d['records']}, сессий: {d['sessions']}. {d['subscription_note']}")
    if d["web_search"]["calls"]:
        lines.append(_web_search(d["web_search"]["calls"], d["web_search"]["usd"]) + "; входит в сумму выше.")
    if d["total_usd"] is not None and d["breakdown_usd"] is not None:
        if abs(d["total_usd"] - d["breakdown_usd"]) > 1e-6:
            lines.append(
                f"Разбивка ниже — оценка по транскриптам ({_usd(d['breakdown_usd'])}), "
                f"итог проекта — {d['precision']} ({_usd(d['total_usd'])})."
            )
    if d["prices_accessed"] and d["precision"] == "оценка":
        lines.append(f"Цены — таблица config/prices.yaml, обращение к странице цен {d['prices_accessed']}.")
    for title, key, name in (("По ролям", "roles", "role"), ("По шагам", "steps", "step")):
        if not d[key]:
            continue
        lines.append(f"{title}:")
        for row in d[key]:
            models = ", ".join(row["models"])
            lines.append(
                f"  {row[name]}: {_sum(row)}, {_share(row['share'])}, вызовов {row['calls']}; "
                f"{models}; {_tokens(row)}"
            )
    if d["models"]:
        lines.append("По моделям:")
        for row in d["models"]:
            lines.append(f"  {row['model']}: {_sum(row)}, {_share(row['share'])}, вызовов {row['calls']}")
    if d["unpriced_models"]:
        names = ", ".join(f"{u['model']} ({u['calls']})" for u in d["unpriced_models"])
        lines.append(f"Модели без цены в config/prices.yaml: {names}; токены записаны, сумма «нет цены».")
    if d["skipped_records"]:
        lines.append(f"Пропущено при разборе транскриптов: {d['skipped_records']} (битые строки, нет usage).")
    if d["model_upgrades"]:
        lines.append("Повышения модели:")
        for u in d["model_upgrades"]:
            lines.append(f"  шаг {u['step']}: {u['role']} -> {u['model']}; {u['reason'] or 'без причины'}")
    return lines


def cmd_cost_report(args: argparse.Namespace) -> int:
    p = _open(args)
    data = report_data(p)
    return emit(args, data, report_lines(data))
