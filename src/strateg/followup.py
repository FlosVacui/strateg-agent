"""Сопровождение решений после выпуска (ADR-0016): `due`, `metric add`, `impact`, `diff`.

- `strateg due [--today ДАТА] [--json]` — что решать сейчас: срок пересмотра, наступивший срок условия
  отказа, сработавшее условие, устаревшие основания, обязательства со сроком. Только чтение.
- `strateg metric add ПП-n --value ЧИСЛО [ЕДИНИЦА] --date ДАТА [--note ОТКУДА]` — значение показателя,
  которое измерил человек; сравнивается с порогом условий отказа этой строки плана проверки.
- `strateg impact И-n|У-n [--json]` — что задевает новое сведение по цепочке связей. Только чтение.
- `strateg diff [--release N] [--json]` — «было → стало» после выпуска. Только чтение.

Чтение не пишет в журнал: пауза на воротах и после остановки от этих команд не меняется.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from strateg import budget as budgetlib
from strateg import outputs
from strateg import project as proj
from strateg import release as releaselib
from strateg import thresholds as th
from strateg.commands import _find_claim, _open, emit
from strateg.models import (
    Claim,
    Decision,
    DecisionCondition,
    MetricsFile,
    MetricValue,
    OptionsFile,
    latin_hint,
)
from strateg.project import CliError, Project, UsageError
from strateg.project_check import _FRAME_FIELDS, METRICS_FILE, VERIFICATION_FILE

NOTHING_NOW = "Решать сейчас нечего."
NO_DECISIONS = "Решений нет: сопровождение начинается после выпуска (шаг 7)."
STOP_TEXT = "внеплановая остановка: спросить человека до пересчёта"


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------


def _n(ref: str) -> int:
    """Число из номера `Реш-12` — для порядка."""
    try:
        return int(ref.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _sorted_ids(ids: Any) -> list[str]:
    return sorted(set(ids), key=lambda r: (r.split("-")[0], _n(r)))


def _read(p: Project, kind: str) -> Any:
    """Файл выхода как модель; нет файла — None; файл не по схеме — отказ с советом запустить `check`."""
    spec = outputs.KINDS[kind]
    path = p.root / spec.file
    if not path.is_file():
        return None
    try:
        return spec.model.model_validate(proj.read_yaml(path), by_name=False)
    except ValidationError as exc:
        raise CliError(f"{spec.file} не по схеме: запустите `strateg check` и исправьте ошибки") from exc


def load_metrics(p: Project) -> MetricsFile:
    path = p.root / METRICS_FILE
    if not path.is_file():
        return MetricsFile(schema_version=1, values=[])
    try:
        return MetricsFile.model_validate(proj.read_yaml(path))
    except ValidationError as exc:
        raise CliError(f"{METRICS_FILE} не по схеме: запустите `strateg check` и исправьте ошибки") from exc


def _parse_day(raw: str) -> dt.date:
    """Дата вида 2026-11-06; другое — ошибка использования."""
    text = raw.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return dt.date.fromisoformat(text)
    except ValueError:
        pass
    raise UsageError(f"дата «{raw}» не подходит: нужна дата вида 2026-11-06 (год-месяц-день)")


def _today(args: argparse.Namespace) -> dt.date:
    raw = getattr(args, "today", None)
    return proj.now().date() if raw is None else _parse_day(raw)


def _day(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _in_days(delta: int) -> str:
    if delta == 0:
        return "сегодня"
    if delta > 0:
        return f"через {delta} дн."
    return f"просрочено на {-delta} дн."


def latest_value(
    metrics: MetricsFile,
    row: str | None,
    *,
    today: dt.date | None = None,
    since: dt.date | None = None,
) -> MetricValue | None:
    """Последнее значение строки: с самой поздней датой, при равных датах — записанное позже.

    Значения позже `today` не берутся (срез «на дату»); значения раньше `since` (дня решения) тоже.
    """
    if row is None:
        return None
    best: MetricValue | None = None
    for v in metrics.values:
        if v.row != row or (today is not None and v.date > str(today)):
            continue
        if since is not None and v.date < str(since):
            continue
        if best is None or v.date >= best.date:
            best = v
    return best


def value_before(
    metrics: MetricsFile, row: str | None, *, today: dt.date, since: dt.date | None
) -> MetricValue | None:
    """Последнее значение строки на дату раньше дня решения: на условие этого решения не засчитывается."""
    if row is None or since is None:
        return None
    older = [v for v in metrics.values if v.row == row and v.date <= str(today) and v.date < str(since)]
    return max(older, key=lambda v: v.date, default=None)


def _decision_versions(p: Project) -> list[set[str]] | None:
    """Номера решений во всех версиях `decisions.yaml`, от первой к текущей; нет цельной истории — None."""
    folder = p.root / "history"
    found: dict[int, Path] = {}
    if folder.is_dir():
        for path in folder.rglob("*"):
            source = proj.history_source(path.relative_to(p.root).as_posix()) if path.is_file() else None
            if source is not None and source[0] == "decisions.yaml":
                found[source[1]] = path
    if sorted(found) != list(range(1, len(found) + 1)):
        return None
    out: list[set[str]] = []
    try:
        for path in [*(found[n] for n in sorted(found)), p.root / "decisions.yaml"]:
            data = proj.read_yaml(path)
            out.append({str(x["id"]) for x in data.get("decisions", [])})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CliError):
        return None
    return out


def decision_day(p: Project, d: Decision) -> dt.date | None:
    """День, когда решение попало в проект: событие журнала `strateg write decisions`, где оно впервые есть.

    Поля с датой в решении нет, поэтому день берётся из журнала по версиям файла (текущая версия — последняя
    запись, `history/decisions.v<n>.yaml` — прежние). Решение, которое записано не командой `write`
    или раньше начала журнала, даёт None: тогда значения показателей по дате решения не отсекаются.
    """
    events = [
        e
        for e in p.journal
        if e.kind == "действие" and e.name == "strateg write decisions" and e.status == "выполнено"
    ]
    versions = _decision_versions(p)
    if not events or not versions:
        return None
    first = next((j for j, ids in enumerate(versions) if d.id in ids), None)
    if first is None:
        return None
    index = len(events) - (len(versions) - first)
    if index < 0:
        return None
    return events[index].time.astimezone(proj.now().tzinfo).date()


@dataclass(frozen=True)
class Verdict:
    state: str  # «сработало», «не сработало», «порог не разобран», «единицы разные», «нет значения»
    threshold: th.Threshold | None = None


def judge(cond: DecisionCondition, value: MetricValue | None) -> Verdict:
    """Значение против порога условия по числу и знаку; не уверены — просим сравнить вручную."""
    if value is None:
        return Verdict("нет значения")
    t = th.parse_threshold(cond.threshold)
    if t is None:
        return Verdict("порог не разобран")
    if t.unit != value.unit:
        return Verdict("единицы разные", t)
    return Verdict("сработало" if th.fires(t, value.value) else "не сработало", t)


# ---------------------------------------------------------------------------
# strateg due
# ---------------------------------------------------------------------------

GROUPS = (
    ("review", "Пора пересматривать решение"),
    ("check", "Пора проверить показатель: наступил срок условия отказа"),
    ("fired", "Сработало условие отказа"),
    ("manual", "Показатель внесён, но сравнить с порогом нельзя: сравните вручную"),
    ("stale", "Устарели основания решения"),
    ("commitments", "Обязательства: срок близко или прошёл"),
)


def _item(d: Decision, text: str, **extra: Any) -> dict[str, Any]:
    return {"decision": d.id, "text": text, **extra}


def _value_text(v: MetricValue) -> str:
    return th.show_amount(v.value, v.unit)


def _condition_items(
    d: Decision,
    c: DecisionCondition,
    i: int,
    metrics: MetricsFile,
    today: dt.date,
    since: dt.date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"check": [], "fired": [], "manual": []}
    label = f"условие {c.row}" if c.row else f"условие №{i + 1}"
    value = latest_value(metrics, c.row, today=today, since=since)
    early = value_before(metrics, c.row, today=today, since=since) if value is None else None
    verdict = judge(c, value)
    base = {"row": c.row, "metric": c.metric, "threshold": c.threshold, "action": c.action, "date": c.date}
    if value is not None:
        base |= {"value": value.value, "unit": value.unit, "value_date": value.date, "note": value.note}
    if verdict.state == "сработало" and value is not None:
        by = f" ({value.note})" if value.note else ""
        out["fired"].append(
            _item(
                d,
                f"{d.id}: {label} сработало — {c.metric}: {_value_text(value)} при пороге «{c.threshold}», "
                f"внесено {value.date}{by}. Действие по условию: {c.action}.",
                **base,
            )
        )
    elif verdict.state in ("порог не разобран", "единицы разные") and value is not None:
        why = (
            f"порог «{c.threshold}» не разобран"
            if verdict.state == "порог не разобран"
            else f"порог «{c.threshold}» и значение в разных единицах"
        )
        out["manual"].append(
            _item(
                d,
                f"{d.id}: {label}: {c.metric}: внесено {_value_text(value)} ({value.date}), но {why}, "
                "сравните вручную.",
                reason=verdict.state,
                **base,
            )
        )
    if verdict.state != "сработало" and _day(c.date) <= today and (value is None or value.date < c.date):
        late = _in_days((_day(c.date) - today).days)
        if value:
            seen = f"; последнее значение {_value_text(value)} от {value.date}"
        elif early:
            seen = (
                f"; значение {_value_text(early)} от {early.date} внесено до решения ({since}) "
                "и на это условие не засчитано"
            )
        else:
            seen = "; значений нет"
        out["check"].append(
            _item(
                d,
                f"{d.id}: {label}: срок {c.date} ({late}) — пора проверить показатель «{c.metric}» "
                f"(порог «{c.threshold}»{seen}).",
                days=(_day(c.date) - today).days,
                **({"before_decision": early.date} if early else {}),
                **base,
            )
        )
    return out


def _walk(d: Decision, claims: dict[str, Claim]) -> list[tuple[str, str, Claim, list[str]]]:
    """Утверждения, на которых стоит решение: (роль, прямая опора решения, утверждение, цепочка до него).

    Идём от оснований и допущений решения вглубь по `basis` утверждений. Ниже отозванного или заменённого
    не спускаемся: о нём сказано, а то, что под ним, уже не держит решение. Повторы и циклы отсекаются.
    """
    out: list[tuple[str, str, Claim, list[str]]] = []
    for role, refs in (("основание", d.basis), ("допущение", d.assumptions)):
        for root in refs:
            seen: set[str] = set()
            queue: deque[tuple[str, list[str]]] = deque([(root, [root])])
            while queue:
                cid, path = queue.popleft()
                c = claims.get(cid)
                if c is None or cid in seen:
                    continue
                seen.add(cid)
                out.append((role, root, c, path))
                if c.status == "действует":
                    queue.extend((b, [*path, b]) for b in c.basis if b not in seen)
    return out


def _accessed_day(s: Any) -> dt.date:
    """День получения источника в поясе проекта (запись могла быть сделана в другом поясе)."""
    return s.fetch.accessed_at.astimezone(proj.now().tzinfo).date()


def _fresh_days(p: Project) -> dict[str, dt.date]:
    """Для каждого веб-источника день последней проверки по всей цепочке перепроверок (`refreshes`).

    Проверка — получение или перепроверка (`fetch --refresh`) любого звена цепочки: утверждения могут стоять
    на старом снимке, а страницу уже сверили позже. Решение «заменить или оставить» агент принимает по ответу
    `fetch --refresh`, `due` после перепроверки не напоминает.
    """
    web = [s for s in p.sources.sources if s.fetch is not None]
    parent = {s.id: s.id for s in web}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in web:
        if s.refreshes in parent:
            parent[root(s.id)] = root(s.refreshes)
    tz = proj.now().tzinfo
    best: dict[str, dt.date] = {}
    for s in web:
        days = [_accessed_day(s)]
        if s.rechecked_at is not None:
            days.append(s.rechecked_at.astimezone(tz).date())
        r = root(s.id)
        best[r] = max(best.get(r, min(days)), *days)
    return {s.id: best[root(s.id)] for s in web}


def _stale_items(d: Decision, p: Project, today: dt.date, max_age: int) -> list[dict[str, Any]]:
    claims = {c.id: c for c in p.claims.claims}
    sources = {s.id: s for s in p.sources.sources}
    fresh = _fresh_days(p)
    out = []
    walked = _walk(d, claims)
    # Устаревший источник факта называем один раз — по ближайшему пути: путей к факту бывает несколько.
    nearest = {}
    for _, _, c, path in walked:
        nearest[c.id] = min(nearest.get(c.id, len(path)), len(path))
    said: set[tuple[str, str]] = set()
    for role, root, c, path in walked:
        cid = c.id
        chain = " → ".join(path)
        deep = len(path) > 1
        extra = {"through": root, "chain": path} if deep else {}
        if c.status in ("заменено", "отозвано"):
            if c.status == "заменено":
                what, reason = f"заменено на {c.replaced_by}", "заменено"
                word = f"заменённом {cid} ({what})"
            else:
                why = c.status_history[-1].reason if c.status_history else None
                what, reason = "отозвано" + (f": {why}" if why else ""), "отозвано"
                word = f"отозванном {cid}" + (f" ({why})" if why else "")
            if deep:
                text = (
                    f"{d.id}: {path[-2]} стоит на {word}"
                    + (f"; цепочка {chain}" if len(path) > 2 else "")
                    + f"; на {root} опирается решение ({role})."
                )
            else:
                text = f"{d.id}: {cid} {what}, а на него опирается решение ({role})."
            extras = {"replaced_by": c.replaced_by} if c.status == "заменено" else {}
            out.append(_item(d, text, claim=cid, role=role, reason=reason, **extras, **extra))
        elif c.type == "факт":
            for sid in dict.fromkeys(q.source for q in c.quotes):
                s = sources.get(sid)
                if s is None or s.fetch is None:
                    continue  # у материала из inputs/ даты обращения нет
                got = fresh[sid]
                age = (today - got).days
                if age > max_age and len(path) == nearest[cid] and (cid, sid) not in said:
                    said.add((cid, sid))
                    holder = f"{cid} ({role})" if not deep else f"{cid}, на котором стоит {root} ({role}),"
                    out.append(
                        _item(
                            d,
                            f"{d.id}: {holder} держится на источнике {sid}, "
                            f"{'перепроверенном' if got != _accessed_day(s) else 'полученном'} {got}: "
                            f"прошло {age} дн., допустимо {max_age}; перепроверьте.",
                            claim=cid,
                            role=role,
                            reason="источник устарел",
                            source=sid,
                            accessed=str(got),
                            age_days=age,
                            **extra,
                        )
                    )
    return out


def _commitment_items(d: Decision, today: dt.date, window: int) -> list[dict[str, Any]]:
    out = []
    for cm in d.commitments:
        if cm.done is not None:
            continue  # человек сказал «выполнено»: решать тут нечего
        delta = (_day(cm.due) - today).days
        if delta <= window:
            out.append(
                _item(
                    d,
                    f"{d.id}: обязательство «{cm.what}» — срок {cm.due} ({_in_days(delta)}), "
                    f"владелец: {cm.owner}.",
                    what=cm.what,
                    due=cm.due,
                    owner=cm.owner,
                    days=delta,
                )
            )
    return out


def _next_event(
    active: list[Decision], p: Project, today: dt.date, window: int, max_age: int
) -> dict[str, Any] | None:
    """Когда `due` перестанет быть пустым: ближайшая дата будущего события."""
    found: list[tuple[dt.date, str]] = []
    claims = {c.id: c for c in p.claims.claims}
    sources = {s.id: s for s in p.sources.sources}
    fresh = _fresh_days(p)
    for d in active:
        for _role, _root, c, _path in _walk(d, claims):
            if c.status != "действует" or c.type != "факт":
                continue
            for sid in dict.fromkeys(q.source for q in c.quotes):
                s = sources.get(sid)
                if s is None or s.fetch is None:
                    continue
                day = fresh[sid] + dt.timedelta(days=max_age + 1)
                if day > today:
                    found.append(
                        (day, f"{d.id}: источник {sid} основания {c.id} станет старше {max_age} дн.")
                    )
        if _day(d.review_date) > today:
            found.append((_day(d.review_date), f"{d.id}: срок пересмотра {d.review_date}"))
        for c in d.conditions:
            if _day(c.date) > today:
                what = f"условие {c.row}" if c.row else "условие отказа"
                found.append(
                    (_day(c.date), f"{d.id}: срок проверки показателя «{c.metric}» ({what}) {c.date}")
                )
        for cm in d.commitments:
            if cm.done is None and _day(cm.due) - dt.timedelta(days=window) > today:
                found.append(
                    (
                        _day(cm.due) - dt.timedelta(days=window),
                        f"{d.id}: подходит срок обязательства «{cm.what}» ({cm.due})",
                    )
                )
    if not found:
        return None
    day, what = min(found, key=lambda x: x[0])
    return {"date": str(day), "text": what}


def compute_due(p: Project, today: dt.date) -> dict[str, Any]:
    decisions = _read(p, "decisions")
    follow = budgetlib.load_defaults().follow_up
    metrics = load_metrics(p)
    everything = decisions.decisions if decisions else []
    active = [d for d in everything if d.status == "действует"]
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in GROUPS}
    for d in active:
        if _day(d.review_date) <= today:
            late = _in_days((_day(d.review_date) - today).days)
            groups["review"].append(
                _item(
                    d,
                    f"{d.id}: срок пересмотра {d.review_date} наступил ({late}).",
                    review_date=d.review_date,
                )
            )
        since = decision_day(p, d)
        for i, c in enumerate(d.conditions):
            for key, items in _condition_items(d, c, i, metrics, today, since).items():
                groups[key] += items
        groups["stale"] += _stale_items(d, p, today, follow.source_max_age_days)
        groups["commitments"] += _commitment_items(d, today, follow.commitments_window_days)
    empty = not any(groups.values())
    if not everything:
        message = NO_DECISIONS
    elif not active:
        message = "Действующих решений нет: все заменены или отменены."
    elif empty:
        message = NOTHING_NOW
    else:
        message = f"Что решать сейчас — на {today}:"
    nxt = (
        _next_event(active, p, today, follow.commitments_window_days, follow.source_max_age_days)
        if empty and active
        else None
    )
    return {
        "today": str(today),
        "decisions": len(everything),
        "active": len(active),
        "empty": empty,
        "message": message,
        "groups": groups,
        "next_event": nxt,
    }


def cmd_due(args: argparse.Namespace) -> int:
    p = _open(args)
    data = compute_due(p, _today(args))
    lines = [data["message"]]
    if data["empty"]:
        nxt = data["next_event"]
        if nxt:
            lines.append(f"Ближайшее событие {nxt['date']}: {nxt['text']}.")
    else:
        for key, title in GROUPS:
            items = data["groups"][key]
            if items:
                lines.append(f"{title}:")
                lines += [f"  {i['text']}" for i in items]
    return emit(args, data, lines)


# ---------------------------------------------------------------------------
# strateg metric add
# ---------------------------------------------------------------------------


def _expected_units(p: Project, row_id: str) -> set[str]:
    """Единицы порогов строки плана проверки и условий отказа действующих решений по ней."""
    ver = _read(p, "verification")
    texts = [r.change_mind.threshold for r in (ver.rows if ver else []) if r.id == row_id]
    dec = _read(p, "decisions")
    for d in dec.decisions if dec else []:
        if d.status == "действует":
            texts += [c.threshold for c in d.conditions if c.row == row_id]
    parsed = [th.parse_threshold(t) for t in texts]
    return {t.unit for t in parsed if t is not None and t.unit}


def _check_row_id(text: str) -> str:
    value = text.strip()
    if not re.fullmatch(r"ПП-[1-9][0-9]*", value):
        raise UsageError(
            f"номер строки плана проверки «{value}» не подходит: нужен вид ПП-1 "
            f"(буквы русские){latin_hint(value)}"
        )
    return value


def cmd_metric_add(args: argparse.Namespace) -> int:
    p = _open(args)
    row_id = _check_row_id(args.row)
    parsed = th.parse_value(args.value)
    if parsed is None:
        raise UsageError(
            f"значение «{args.value}» не число: нужно число, при необходимости с единицей: "
            "«18», «18 %», «1500 ₽»"
        )
    number, unit = parsed
    day = _parse_day(args.date)
    if day > proj.now().date():
        raise CliError(f"дата {day} в будущем: показатель вносят на дату, когда он уже измерен")
    ver = _read(p, "verification")
    if ver is None or row_id not in {r.id for r in ver.rows}:
        raise CliError(
            f"строки {row_id} нет в {VERIFICATION_FILE}: показатель вносят по строке плана проверки; "
            "номера строк видны в условиях отказа решения"
        )
    expected = _expected_units(p, row_id)
    if expected and unit is None:
        shown = " или ".join(sorted(expected))
        raise CliError(
            f"у значения нет единицы, а порог строки {row_id} задан в «{shown}»: напишите единицу вместе "
            f"с числом, например --value '{th.show_number(number)} {sorted(expected)[0]}'"
        )
    if expected and unit not in expected:
        raise CliError(
            f"единицы не совпадают: значение в «{unit}», а порог строки {row_id} задан в "
            f"«{' или '.join(sorted(expected))}»; процент и рубли не сравниваются — внесите значение "
            "в единицах порога"
        )
    note = args.note.strip() if args.note and args.note.strip() else None
    shown = th.show_amount(number, unit)
    started = time.monotonic()
    # Чтение, поиск повтора и запись — под одной блокировкой папки: иначе параллельный вызов затрёт запись.
    with proj.locked(p.root):
        metrics = load_metrics(p)
        same = next(
            (
                v
                for v in metrics.values
                if (v.row, v.value, v.unit, v.date) == (row_id, number, unit, str(day))
            ),
            None,
        )
        if same is None:
            record = MetricValue(
                row=row_id,
                value=number,
                unit=unit,  # type: ignore[arg-type]
                date=str(day),
                note=note,
                origin="пользователь",
                recorded_at=proj.now(),
            )
            with proj.transaction(p.root):
                metrics.values.append(record)
                proj.write_text(p.root / METRICS_FILE, proj.dump_yaml(metrics))
                p.add_event(
                    p.event(
                        kind="действие",
                        name="strateg metric add",
                        idempotency_hash=proj.key_hash("metric add", row_id, number, unit, str(day)),
                        status="выполнено",
                        duration_s=round(time.monotonic() - started, 2),
                        message=f"{row_id}: {shown} на {day}",
                    )
                )
                p.save_state()
    if same is not None:
        note = same.note
    # Что значение говорит по условиям отказа этой строки.
    dec = _read(p, "decisions")
    now_day = proj.now().date()
    results = []
    for d in dec.decisions if dec else []:
        if d.status != "действует":
            continue
        since = decision_day(p, d)
        value = latest_value(metrics, row_id, today=now_day, since=since)
        early = value_before(metrics, row_id, today=now_day, since=since) if value is None else None
        for c in d.conditions:
            if c.row != row_id:
                continue
            state = "до решения" if early is not None else judge(c, value).state
            results.append({"decision": d.id, "threshold": c.threshold, "result": state, "action": c.action})
    lines = [
        f"{row_id}: значение {shown} на {day} уже записано, повтор ничего не меняет."
        if same is not None
        else f"{row_id}: значение {shown} на {day} записано (внёс человек)."
    ]
    for r in results:
        tail = {
            "сработало": f"СРАБОТАЛО, действие: {r['action']}",
            "не сработало": "не сработало",
            "порог не разобран": "порог не разобран, сравните вручную",
            "до решения": "значение внесено до решения и на это условие не засчитано",
            "единицы разные": "единицы порога и значения разные, сравните вручную",
        }.get(r["result"], r["result"])
        lines.append(f"  {r['decision']}: порог «{r['threshold']}» — {tail}.")
    if not results:
        lines.append("  Ни одно действующее решение не ставит условие отказа по этой строке.")
    return emit(
        args,
        {
            "row": row_id,
            "value": number,
            "unit": unit,
            "date": str(day),
            "note": note,
            "already_recorded": same is not None,
            "conditions": results,
        },
        lines,
    )


# ---------------------------------------------------------------------------
# strateg impact
# ---------------------------------------------------------------------------

_SOURCE_REF = re.compile(r"И-[1-9][0-9]*")
_CLAIM_REF = re.compile(r"У-[1-9][0-9]*")


def _add(bucket: dict[str, list[str]], key: str, why: str) -> None:
    if why not in bucket.setdefault(key, []):
        bucket[key].append(why)


def _entries(bucket: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [{"id": k, "why": bucket[k]} for k in _sorted_ids(bucket)]


def _doc_hits(root: Path, numbers: set[str]) -> list[dict[str, Any]]:
    """Документы `outputs/`, в которых встречаются затронутые номера (номер целиком, не часть другого)."""
    folder = root / releaselib.OUTPUTS_DIR
    if not folder.is_dir():
        return []
    patterns = {n: re.compile(rf"(?<![\w-]){re.escape(n)}(?!\d)") for n in numbers}
    out = []
    for path in sorted(folder.rglob("*.md")):
        text = path.read_bytes().decode("utf-8", errors="replace")
        found = _sorted_ids(n for n, rx in patterns.items() if rx.search(text))
        if found:
            out.append({"path": path.relative_to(root).as_posix(), "numbers": found})
    return out


def compute_impact(p: Project, seed: str) -> dict[str, Any]:
    claims: dict[str, Claim] = {c.id: c for c in p.claims.claims}
    plan, gaps = _read(p, "plan"), _read(p, "gaps")
    opts: OptionsFile | None = _read(p, "options")
    ver, prom, dec = _read(p, "verification"), _read(p, "promises"), _read(p, "decisions")
    frame = _read(p, "frame")

    # --- исходное и утверждения по связям `basis`, спору и замене (`replaces`/`replaced_by`) ---
    seed_claims: list[str]
    seed_info: dict[str, Any]
    frame_fields: list[str] = []
    if _SOURCE_REF.fullmatch(seed):
        src = next((s for s in p.sources.sources if s.id == seed), None)
        if src is None:
            raise CliError(f"источника {seed} нет в реестре (sources.yaml)")
        seed_claims = [c.id for c in claims.values() if any(q.source == seed for q in c.quotes)]
        seed_info = {"id": seed, "kind": "источник", "title": src.url or src.path}
        if frame is not None:
            frame_fields = [f for f in _FRAME_FIELDS if getattr(getattr(frame, f), "source", None) == seed]
    else:
        c = _find_claim(p, seed)
        seed_claims = [c.id]
        seed_info = {"id": seed, "kind": "утверждение", "status": c.status, "text": c.text}
        if c.replaced_by:
            seed_info["replaced_by"] = c.replaced_by
    dependents: dict[str, list[str]] = {}
    for cid in seed_claims:
        if _SOURCE_REF.fullmatch(seed):
            _add(dependents, cid, f"цитата из {seed}")
    reached = set(seed_claims)
    queue = list(seed_claims)
    while queue:
        cur = queue.pop()
        for c in claims.values():
            if cur in c.basis:
                _add(dependents, c.id, f"основание {cur}")
            elif c.dispute is not None and c.dispute.claim == cur:
                _add(dependents, c.id, f"спор с {cur}")
            elif c.replaces == cur:  # новая версия: всё, что стояло на заменённом, теперь под вопросом
                _add(dependents, c.id, f"заменяет {cur}")
            elif c.replaced_by == cur:  # прежняя версия: на ней могло стоять то, что ещё не переписано
                _add(dependents, c.id, f"заменено на {cur}")
            else:
                continue
            if c.id not in reached:
                reached.add(c.id)
                queue.append(c.id)
        # Утверждение со спором задевает и оспоренное: прежнее сведение теперь под вопросом, как и всё на нём
        disputed = claims[cur].dispute.claim if cur in claims and claims[cur].dispute else None
        if disputed is not None and disputed in claims:
            _add(dependents, disputed, f"оспаривается {cur}")
            if disputed not in reached:
                reached.add(disputed)
                queue.append(disputed)
    if _CLAIM_REF.fullmatch(seed):
        dependents.pop(seed, None)  # само исходное утверждение в «задетых» не повторяется
    affected = set(seed_claims) | set(dependents)

    # --- вопросы плана и пробелы ---
    questions: dict[str, list[str]] = {}
    gap_why: dict[str, list[str]] = {}
    if plan is not None:
        by_text = {q.text.strip().casefold(): q.id for q in plan.questions}
        ids = {q.id for q in plan.questions}
        for c in claims.values():
            if c.id not in affected or not c.plan_question:
                continue
            value = c.plan_question.strip()
            qid = value if value in ids else by_text.get(value.casefold())
            if qid:
                _add(questions, qid, f"вопрос утверждения {c.id}")
        for q in plan.questions:
            if q.id in questions and q.gap:
                _add(gap_why, q.gap, f"вопрос плана {q.id}")
    coverage: dict[str, list[str]] = {}
    if gaps is not None:
        for g in gaps.gaps:
            for field, ref in (("допущение", g.assumption), ("гипотеза", g.hypothesis)):
                if ref in affected:
                    _add(gap_why, g.id, f"{field} {ref}")
            for ref in g.closed_by:
                if ref in affected:
                    _add(gap_why, g.id, f"закрыт утверждением {ref}")
        for m in gaps.metric_coverage:
            hit = [r for r in (m.claims or []) if r in affected]
            if hit:
                _add(coverage, m.kind, "опора " + ", ".join(hit))
                if m.gap:
                    _add(gap_why, m.gap, f"перечень показателей: «{m.kind}»")

    # --- варианты ---
    options: dict[str, list[str]] = {}
    rec_why: list[str] = []
    if opts is not None:
        for o in opts.options:
            for obj in o.objections:
                if obj.basis in affected:
                    _add(options, o.id, f"возражение, основание {obj.basis}")
            for name, label in (("effect", "эффекта"), ("first_signal", "первого сигнала")):
                ref = getattr(o.comparison, name).forecast
                if ref in affected:
                    _add(options, o.id, f"прогноз {label} {ref}")
        for g in gaps.gaps if gaps else []:
            if g.id in gap_why:
                for o_id in g.affects_options:
                    _add(options, o_id, f"пробел {g.id} влияет на вариант")
        hit = [r for r in opts.recommendation.claims if r in affected]
        if hit:
            rec_why = ["рекомендация опирается на " + ", ".join(hit)]
            _add(options, opts.recommendation.option, rec_why[0])

    # --- строки плана проверки, обещания ---
    rows: dict[str, list[str]] = {}
    if ver is not None:
        for r in ver.rows:
            if r.hypothesis in affected:
                _add(rows, r.id, f"гипотеза {r.hypothesis}")
            if r.baseline in gap_why:
                _add(rows, r.id, f"база сравнения {r.baseline}")
        for m in ver.premortem:
            if m.claim in affected:
                _add(rows, m.row, f"причина пре-мортема {m.claim}")
    promises: dict[str, list[str]] = {}
    if prom is not None:
        for x in prom.promises:
            hit = [r for r in x.claims if r in affected]
            if hit:
                _add(promises, x.id, "опора " + ", ".join(hit))

    # --- решения (действующие) ---
    decisions: dict[str, list[str]] = {}
    basis_hits: list[str] = []
    for d in dec.decisions if dec else []:
        if d.status != "действует":
            continue
        for ref in d.basis:
            if ref in affected:
                _add(decisions, d.id, f"основание {ref}")
                basis_hits.append(f"решение {d.id} опирается на {ref}")
        for ref in d.assumptions:
            if ref in affected:
                _add(decisions, d.id, f"допущение {ref}")
        if d.option in options:
            _add(decisions, d.id, f"выбранный вариант {d.option} задет")
        for ref in d.rejected:
            if ref in options:
                _add(decisions, d.id, f"отвергнутый вариант {ref} задет")
        for c in d.conditions:
            if c.row in rows:
                _add(decisions, d.id, f"условие отказа по строке {c.row}")

    # --- выбор на В4 ---
    via: list[str] = list(rec_why) + basis_hits
    if opts is not None:
        for o_id in dict.fromkeys([opts.recommendation.option, *([opts.chosen] if opts.chosen else [])]):
            other = [w for w in options.get(o_id, []) if w not in rec_why]
            if other:
                via.append(f"вариант {o_id} задет: " + "; ".join(other))
    numbers = (
        {seed}
        | affected
        | set(questions)
        | set(gap_why)
        | set(options)
        | set(rows)
        | set(promises)
        | set(decisions)
    )
    result: dict[str, Any] = {
        "seed": seed_info,
        "seed_claims": seed_claims if _SOURCE_REF.fullmatch(seed) else [],
        "claims": _entries(dependents),
        "frame_fields": frame_fields,
        "plan_questions": _entries(questions),
        "gaps": _entries(gap_why),
        "metric_coverage": [{"kind": k, "why": v} for k, v in coverage.items()],
        "options": _entries(options),
        "promises": _entries(promises),
        "rows": _entries(rows),
        "decisions": _entries(decisions),
        "documents": _doc_hits(p.root, numbers),
        "choice": {"affected": bool(via), "via": via, "stop": STOP_TEXT if via else None},
    }
    return result


IMPACT_TITLES = (
    ("claims", "Утверждения"),
    ("plan_questions", "Вопросы плана"),
    ("gaps", "Пробелы"),
    ("options", "Варианты"),
    ("promises", "Обещания"),
    ("rows", "Строки плана проверки"),
    ("decisions", "Решения"),
)


def _impact_lines(r: dict[str, Any]) -> list[str]:
    seed = r["seed"]
    if seed["kind"] == "источник":
        head = f"Источник {seed['id']} ({seed['title']})."
        head += (
            f" Утверждения с цитатой из него: {', '.join(r['seed_claims'])}."
            if r["seed_claims"]
            else " Ни одно утверждение на цитату из него не опирается."
        )
    else:
        status = f"{seed['status']}" + (f" на {seed['replaced_by']}" if seed.get("replaced_by") else "")
        head = f"Утверждение {seed['id']} ({status}): {seed['text']}"
    lines = [head, "Задето по цепочке связей:"]
    if r["frame_fields"]:
        lines.append(f"  Рамка: поля {', '.join(r['frame_fields'])}")
    for key, title in IMPACT_TITLES:
        if r[key]:
            lines.append(f"  {title}: " + "; ".join(f"{e['id']} ({', '.join(e['why'])})" for e in r[key]))
    if r["metric_coverage"]:
        lines.append(
            "  Перечень показателей: "
            + "; ".join(f"«{e['kind']}» ({', '.join(e['why'])})" for e in r["metric_coverage"])
        )
    if r["documents"]:
        lines.append(
            "  Документы: " + "; ".join(f"{d['path']} ({', '.join(d['numbers'])})" for d in r["documents"])
        )
    if len(lines) == 2:
        lines.append("  ничего, кроме самого исходного")
    ch = r["choice"]
    if ch["affected"]:
        lines.append("Выбор на В4: задет — " + "; ".join(ch["via"]) + ".")
        lines.append(f"Дальше: {ch['stop']} (порядок пересчёта, п. 3).")
    else:
        lines.append("Выбор на В4: не задет.")
    return lines


def _impact_id(text: str) -> str:
    value = text.strip()
    if not (_SOURCE_REF.fullmatch(value) or _CLAIM_REF.fullmatch(value)):
        raise UsageError(
            f"номер «{value}» не подходит: нужен источник вида И-1 или утверждение вида У-1 "
            f"(буква русская){latin_hint(value)}"
        )
    return value


def cmd_impact(args: argparse.Namespace) -> int:
    p = _open(args)
    result = compute_impact(p, _impact_id(args.id))
    return emit(args, result, _impact_lines(result))


# ---------------------------------------------------------------------------
# strateg diff
# ---------------------------------------------------------------------------

_CUT = 90


_TITLE_KEYS = ("title", "text", "what", "metric", "hits_assumption", "kind", "claim", "option")


def _brief(value: Any) -> str:
    """Значение поля для строки «было → стало»: список — через «; », запись — по главному полю."""
    if value is None:
        return "нет"
    if isinstance(value, dict):
        key = next((k for k in _TITLE_KEYS if isinstance(value.get(k), str)), None)
        if key is None:
            return ", ".join(f"{k}: {_brief(v)}" for k, v in value.items())
        extra = value.get("threshold") or value.get("due")
        return f"{value[key]} ({extra})" if extra else str(value[key])
    if isinstance(value, list):
        return "; ".join(_brief(v) for v in value) if value else "пусто"
    return str(value)


def _short(value: Any) -> str:
    text = re.sub(r"\s+", " ", _brief(value))
    return text if len(text) <= _CUT else text[: _CUT - 1] + "…"


def _file_lines(f: releaselib.FileDiff) -> list[str]:
    lines = [f"{f.title}:"]
    if f.added:
        lines.append("  добавлено: " + ", ".join(f.added))
    if f.removed:
        lines.append("  убрано: " + ", ".join(f.removed))
    for ch in f.changed:
        for fld in ch.fields:
            lines.append(f"  {ch.id}, {fld['field']}: было {_short(fld['was'])} → стало {_short(fld['now'])}")
    for b in f.blocks:
        lines.append(f"  {b['block']}: было {_short(b['was'])} → стало {_short(b['now'])}")
    return lines


def _doc_lines(d: releaselib.DocDiff) -> str:
    parts = []
    if d.status != "изменён":
        parts.append(d.status)
    for label, names in (
        ("изменены разделы", d.changed),
        ("добавлены разделы", d.added),
        ("убраны разделы", d.removed),
    ):
        if names:
            parts.append(label + " " + ", ".join(f"«{n}»" for n in names))
    return f"  {d.path}: " + "; ".join(parts)


def cmd_diff(args: argparse.Namespace) -> int:
    p = _open(args)
    snaps = releaselib.release_dirs(p.root)
    want = getattr(args, "release", None)
    if not snaps:
        raise CliError(
            "снимка выпуска нет: он появляется при `strateg step done 7`; проект выпущен раньше "
            "или шаг 7 ещё не завершён"
        )
    if want is None:
        want = max(snaps)
    elif want not in snaps:
        raise CliError(f"снимка выпуска {want} нет; есть: {', '.join(str(n) for n in snaps)}")
    diff = releaselib.compare(p.root, want)
    data = {
        "release": want,
        "changed": not diff.empty(),
        "files": [
            {
                "file": f.file,
                "title": f.title,
                "added": f.added,
                "removed": f.removed,
                "changed": [{"id": c.id, "fields": c.fields} for c in f.changed],
                "blocks": f.blocks,
            }
            for f in diff.files
        ],
        "documents": [
            {"path": d.path, "status": d.status, "added": d.added, "removed": d.removed, "changed": d.changed}
            for d in diff.docs
        ],
    }
    if diff.empty():
        return emit(args, data, ["Изменений после выпуска нет."])
    lines = [f"Изменения после выпуска {want} (было → стало):"]
    for f in diff.files:
        lines += _file_lines(f)
    if diff.docs:
        lines.append("Документы:")
        lines += [_doc_lines(d) for d in diff.docs]
    return emit(args, data, lines)
