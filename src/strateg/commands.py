"""Команды CLI, которые меняют и показывают состояние проекта (ADR-0002).

Каждая команда: открыть проект, внутри `transaction` изменить файлы только через модели, вернуть код
(0 — успех, 1 — ошибка данных или правил, 2 — ошибка использования). Сообщения — по-русски, с
`--json` — машинный вывод.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from strateg import fetch as fetchlib
from strateg import project as proj
from strateg import quote as quotelib
from strateg import safety
from strateg.models import (
    LOCATOR_KINDS,
    SCHEMA_VERSION,
    Claim,
    ClaimId,
    ClaimType,
    CostRecord,
    FetchInfo,
    ForecastRange,
    GateEvent,
    JournalEvent,
    Origin,
    PublisherKind,
    Quote,
    QuoteCheck,
    SourceId,
    StatusChange,
    Step,
    Text,
    norm,
    normalize_quote,
)
from strateg.models import Source as SourceModel
from strateg.project import CliError, Project, UsageError
from strateg.project_check import _explain, _where, check_project

STEP_NAMES = {
    "1": "Постановка",
    "2а": "Исследование: план",
    "2б": "Сбор",
    "3": "Картина ситуации",
    "4": "Варианты и возражения",
    "5": "Стратегии и план проверки",
    "6": "Проверка полноты",
    "7": "Выпуск",
    "8": "Сопровождение",
}
GATES = ("В1", "В2", "В3", "В4", "В5")


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------


def emit(args: argparse.Namespace, data: dict[str, Any], lines: list[str]) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, **data}, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return 0


def fail(args: argparse.Namespace, err: CliError) -> int:
    code = 2 if isinstance(err, UsageError) else 1
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": err.message, "details": err.details}, ensure_ascii=False))
    else:
        print(f"Ошибка: {err.message}", file=sys.stderr)
        for d in err.details:
            print(f"  - {d}", file=sys.stderr)
    return code


def run(args: argparse.Namespace) -> int:
    """Вызов команды с единым разбором ошибок."""
    try:
        return args.handler(args)
    except CliError as err:
        return fail(args, err)


def _num(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _usd(x: float) -> str:
    return f"${x:.2f}".replace(".", ",")


def _open(args: argparse.Namespace) -> Project:
    return Project.load(proj.resolve_project(getattr(args, "project", None)))


def _norm_step(text: str) -> str:
    """Шаг в одном виде; латинские a и b (из Bash) читаются как русские а и б."""
    value = text.strip().lower().translate(str.maketrans({"a": "а", "b": "б"}))
    if value not in STEP_NAMES:
        raise UsageError(f"шаг «{text}» не бывает: допустимо {', '.join(STEP_NAMES)}")
    return value


def _norm_gate(text: str) -> str:
    """Ворота В1…В5; латинская B читается как русская В."""
    value = text.strip().upper().translate(str.maketrans({"B": "В"}))
    if value not in GATES:
        raise UsageError(f"ворота «{text}» не бывают: допустимо {', '.join(GATES)}")
    return value


# ---------------------------------------------------------------------------
# new, use
# ---------------------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> int:
    p = proj.create_project(args.id, args.mode, args.region)
    proj.set_active(args.id)
    errors = check_project(p.root)
    if errors:
        return fail(args, CliError("созданный проект не прошёл проверку", errors))
    return emit(
        args,
        {"project": p.id, "mode": args.mode, "path": str(p.root), "active": True},
        [
            f"Проект {p.id} создан и выбран активным (режим: {args.mode}).",
            f"Папка: {p.root}",
            "Дальше: `strateg step start 1`.",
        ],
    )


def cmd_use(args: argparse.Namespace) -> int:
    root = proj.projects_root() / proj.check_project_id(args.id)
    if not (root / "state.yaml").is_file():
        raise CliError(f"проект {args.id} не найден: создайте его командой `strateg new {args.id}`")
    proj.set_active(args.id)
    return emit(args, {"project": args.id, "active": True}, [f"Активный проект: {args.id}."])


# ---------------------------------------------------------------------------
# Шаги и ворота
# ---------------------------------------------------------------------------


def _open_steps(p: Project) -> dict[str, JournalEvent]:
    """Шаги, начатые и не закрытые: ключ идемпотентности -> событие «начато»."""
    last: dict[str, JournalEvent] = {}
    for e in p.journal:
        if e.kind == "действие" and (e.name or "").startswith("шаг ") and e.idempotency_hash:
            last[e.idempotency_hash] = e
    return {h: e for h, e in last.items() if e.status == "начато"}


def _attempts(p: Project, step: str) -> int:
    name = proj.step_action_name(step)
    return sum(1 for e in p.journal if e.kind == "действие" and e.name == name and e.status == "начато")


def _refresh_budget(p: Project) -> None:
    p.state.budget.spent_minutes = round(proj.work_seconds(p.journal) / 60, 2)
    p.state.budget.spent_usd = proj.cost_total(p.costs)[0]


def cmd_step_start(args: argparse.Namespace) -> int:
    p = _open(args)
    step = _norm_step(args.step)
    notes: list[str] = []
    with proj.transaction(p.root):
        for h, e in _open_steps(p).items():
            p.add_event(
                p.event(
                    step=e.step,
                    kind="действие",
                    name=e.name,
                    idempotency_hash=h,
                    status="прервано",
                    message="шаг начат заново без отметки выполнения"
                    if e.step == step
                    else f"начат шаг {step}, а этот не завершён",
                )
            )
            notes.append(f"Шаг {e.step} не был завершён: записано «прервано».")
        attempt = _attempts(p, step) + 1
        p.add_event(
            p.event(
                step=step,
                kind="действие",
                name=proj.step_action_name(step),
                idempotency_hash=proj.key_hash("step", step, attempt),
                status="начато",
            )
        )
        p.state.step = step
        p.state.next_action = (
            f"Выполнить шаг {step} ({STEP_NAMES[step]}); по завершении `strateg step done {step}`"
        )
        p.save_state()
    return emit(
        args,
        {"step": step, "attempt": attempt, "interrupted": len(notes)},
        [*notes, f"Шаг {step} ({STEP_NAMES[step]}) начат, попытка {attempt}."],
    )


def cmd_step_done(args: argparse.Namespace) -> int:
    p = _open(args)
    step = _norm_step(args.step)
    name = proj.step_action_name(step)
    mine = [e for e in p.journal if e.kind == "действие" and e.name == name and e.idempotency_hash]
    if not mine:
        raise CliError(f"шаг {step} не начат: выполните `strateg step start {step}`")
    last_hash = mine[-1].idempotency_hash
    last = [e for e in mine if e.idempotency_hash == last_hash][-1]
    if last.status == "выполнено":
        return emit(
            args,
            {"step": step, "already_done": True},
            [f"Шаг {step} уже отмечен выполненным."],
        )
    if last.status != "начато":
        raise CliError(
            f"шаг {step} не начат заново после «{last.status}»: выполните `strateg step start {step}`"
        )
    start = next(e for e in mine if e.idempotency_hash == last_hash and e.status == "начато")
    end = proj.now()
    total = max((end - start.time).total_seconds(), 0.0)
    waited = sum(
        proj.overlap_seconds(start.time, end, opened, answered or end)
        for opened, answered in proj.gate_waits(p.journal)
    )
    duration = round(max(total - waited, 0.0), 1)
    with proj.transaction(p.root):
        p.add_event(
            p.event(
                time=end,
                step=step,
                kind="действие",
                name=name,
                idempotency_hash=last_hash,
                status="выполнено",
                duration_s=duration,
            )
        )
        p.state.step = step
        p.state.next_action = args.next or f"Шаг {step} завершён; начать следующий шаг"
        _refresh_budget(p)
        p.save_state()
    return emit(
        args,
        {"step": step, "duration_s": duration, "waited_s": round(waited, 1)},
        [f"Шаг {step} ({STEP_NAMES[step]}) выполнен за {_num(duration / 60)} мин (без ожидания на воротах)."],
    )


def cmd_gate_open(args: argparse.Namespace) -> int:
    p = _open(args)
    gate = _norm_gate(args.gate)
    if args.questions < 0:
        raise UsageError("число вопросов не может быть отрицательным")
    if p.state.gates[gate] == "ждут ответа":  # type: ignore[index]
        return emit(
            args,
            {"gate": gate, "status": "ждут ответа", "already_open": True},
            [f"Ворота {gate} уже открыты."],
        )
    status = "пройдены автоматически" if args.questions == 0 else "ждут ответа"
    with proj.transaction(p.root):
        opened = proj.now()
        p.add_event(
            p.event(
                time=opened,
                kind="ворота",
                gate=GateEvent(gate=gate, mode=p.state.mode, questions=args.questions, opened_at=opened),  # type: ignore[arg-type]
            )
        )
        p.state.gates[gate] = status  # type: ignore[index]
        p.save_state()
    text = (
        f"Ворота {gate} открыты, вопросов: {args.questions}."
        if args.questions
        else f"Ворота {gate} пройдены без вопросов."
    )
    return emit(args, {"gate": gate, "status": status, "questions": args.questions}, [text])


def cmd_gate_answer(args: argparse.Namespace) -> int:
    p = _open(args)
    gate = _norm_gate(args.gate)
    answer = args.answer
    if not answer.strip():
        raise UsageError('ответ пустой: укажите --answer "<текст>"')
    events = [e for e in p.journal if e.gate is not None and e.gate.gate == gate]
    status = p.state.gates[gate]  # type: ignore[index]
    if status == "отвечено" and events and events[-1].gate and events[-1].gate.answer == answer:
        return emit(
            args,
            {"gate": gate, "status": status, "already_answered": True},
            [f"Ответ на ворота {gate} уже записан."],
        )
    opened = next((e.gate for e in reversed(events) if e.gate and e.gate.answered_at is None), None)
    if status != "ждут ответа" or opened is None:
        raise CliError(
            f"ворота {gate} не ждут ответа (статус: {status}): "
            f"откройте их командой `strateg gate open {gate} --questions N`"
        )
    with proj.transaction(p.root):
        answered = proj.now()
        p.add_event(
            p.event(
                time=answered,
                kind="ворота",
                gate=GateEvent(
                    gate=gate,  # type: ignore[arg-type]
                    mode=opened.mode,
                    questions=opened.questions,
                    opened_at=opened.opened_at,
                    answered_at=max(answered, opened.opened_at),
                    answer=answer,
                ),
            )
        )
        p.state.gates[gate] = "отвечено"  # type: ignore[index]
        p.save_state()
    return emit(args, {"gate": gate, "status": "отвечено"}, [f"Ответ на ворота {gate} записан."])


def cmd_mode(args: argparse.Namespace) -> int:
    p = _open(args)
    mode = args.mode.strip()
    if p.state.mode == mode:
        return emit(args, {"mode": mode, "changed": False}, [f"Режим уже «{mode}»."])
    with proj.transaction(p.root):
        old = p.state.mode
        p.add_event(
            p.event(kind="смена режима", mode=mode, message=f"режим изменён: {old} -> {mode}")  # type: ignore[arg-type]
        )
        p.state.mode = mode  # type: ignore[assignment]
        p.save_state()
    return emit(args, {"mode": mode, "changed": True, "previous": old}, [f"Режим изменён: {old} -> {mode}."])


def cmd_stop(args: argparse.Namespace) -> int:
    p = _open(args)
    reason = args.reason.strip()
    if not reason:
        raise UsageError('причина остановки пустая: укажите --reason "<текст>"')
    stops = sum(1 for e in p.journal if e.kind == "действие" and e.name == "внеплановая остановка")
    with proj.transaction(p.root):
        p.add_event(
            p.event(
                kind="действие",
                name="внеплановая остановка",
                idempotency_hash=proj.key_hash("stop", stops + 1),
                status="выполнено",
                duration_s=0,
                message=reason,
            )
        )
        p.save_state()
    return emit(args, {"stops": stops + 1, "reason": reason}, [f"Внеплановая остановка записана: {reason}"])


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def make_fetcher(cache_file: Path) -> fetchlib.Fetcher:
    """Загрузчик проекта. Отдельная функция: тесты подставляют сеть-заглушку."""
    return fetchlib.Fetcher(cache_file)


def _find_open_action(p: Project, h: str) -> bool:
    last = None
    for e in p.journal:
        if e.kind == "действие" and e.idempotency_hash == h:
            last = e
    return last is not None and last.status == "начато"


def _next_source_number(p: Project) -> int:
    return max((int(s.id.split("-")[1]) for s in p.sources.sources), default=0) + 1


def cmd_fetch(args: argparse.Namespace) -> int:
    p = _open(args)
    try:
        url = fetchlib.normalize_url(args.url)
    except fetchlib.AddressRefused as exc:
        raise UsageError(str(exc)) from exc
    region = args.region or p.state.region
    existing = [
        s for s in p.sources.sources if s.url == url and ((s.fetch.region if s.fetch else None) == region)
    ]
    cached = next((s for s in existing if s.access == "получен"), None)
    if cached is not None:
        return _fetch_report(args, cached, p, cached=True, warnings=[], extra={})

    h = proj.key_hash("fetch", url, region)
    started = time.monotonic()
    warnings: list[str] = []
    extra: dict[str, Any] = {}
    with proj.transaction(p.root, rollback=False):
        if _find_open_action(p, h):
            p.add_event(
                p.event(
                    kind="действие",
                    name="strateg fetch",
                    idempotency_hash=h,
                    status="прервано",
                    message="прошлая загрузка не закончилась; повтор",
                )
            )
        p.add_event(p.event(kind="действие", name="strateg fetch", idempotency_hash=h, status="начато"))
        fetcher = make_fetcher(p.root / proj.CACHE_DIR / "fetch.json")
        access, note, final_url, page = "получен", None, None, None
        refused: str | None = None
        try:
            page = fetcher.get(url)
            final_url = page.url
        except fetchlib.AddressRefused as exc:
            refused = str(exc)
        except fetchlib.RobotsDenied as exc:
            access = "запрещено robots.txt"
            note = (
                f"robots.txt сайта запрещает загрузку {exc.url} для strateg; сохраните страницу вручную "
                "в папку inputs/ проекта и добавьте её как материал"
            )
        except fetchlib.FetchProblem as exc:
            access, note = exc.access, exc.note

        if refused is not None:
            p.add_event(
                p.event(
                    kind="действие",
                    name="strateg fetch",
                    idempotency_hash=h,
                    status="ошибка",
                    message=refused,
                )
            )
            p.save_state()
        else:
            source = _store_result(
                p, h, started, url, region, existing, access, note, final_url, page, warnings, extra
            )
    if refused is not None:
        raise CliError(refused)
    for err in check_project(p.root):
        warnings.append("проверка проекта: " + err)
    return _fetch_report(args, source, p, cached=False, warnings=warnings, extra=extra)


def _store_result(
    p: Project,
    h: str,
    started: float,
    url: str,
    region: str | None,
    existing: list[SourceModel],
    access: str,
    note: str | None,
    final_url: str | None,
    page: fetchlib.Page | None,
    warnings: list[str],
    extra: dict[str, Any],
) -> SourceModel:
    """Записывает итог загрузки: снимок, источник в реестре, события журнала."""
    kind = "страница"
    snapshot = None
    if page is not None and access == "получен":
        access, note, kind, snapshot = _analyse(page, extra)

    number = _next_source_number(p)
    replace_at = next((i for i, s in enumerate(p.sources.sources) if s in existing), None)
    if replace_at is not None:
        number = int(p.sources.sources[replace_at].id.split("-")[1])
    sid = f"И-{number}"
    accessed = proj.now()
    info = FetchInfo(accessed_at=accessed, final_url=final_url, region=region)
    data: dict[str, Any] = {
        "id": sid,
        "kind": kind,
        "url": url,
        "access": access,
        "access_note": note,
        "untrusted": True,
    }
    status = "ошибка" if access != "получен" else "выполнено"
    if access == "запрещено robots.txt":
        status = "пропущено"
    if snapshot is not None and page is not None:
        rep, text, encoding = snapshot
        rel = f"sources/i-{number}.txt"
        data_bytes = text.encode("utf-8")
        proj.write_text(p.root / rel, text)
        info = FetchInfo(
            accessed_at=accessed,
            final_url=page.url,
            region=region,
            encoding=encoding,
            extractor=fetchlib.extractor_name(kind),
            raw_sha256=proj.sha256_bytes(page.body),
        )
        data.update(
            fetch=info,
            text_path=rel,
            text_sha256=proj.sha256_bytes(data_bytes),
            suspicious=bool(rep.instruction_signs),
            anonymized=rep.anonymized,
        )
        if rep.anonymized:
            warnings.append("в тексте заменены на метки: " + ", ".join(rep.anonymized))
    else:
        data["fetch"] = info
    source = SourceModel.model_validate(data)
    if replace_at is not None:
        p.sources.sources[replace_at] = source
    else:
        p.sources.sources.append(source)
    p.save_sources()
    if source.suspicious:
        signs = snapshot[0].instruction_signs if snapshot else []
        p.add_event(
            p.event(
                kind="найдена инструкция в источнике",
                source=source.id,
                message="в тексте снимка найдено обращение к ИИ, оно не исполнено (признаки: "
                + "; ".join(signs)
                + ")",
            )
        )
        warnings.append(
            "в тексте страницы найдено обращение к ИИ: источник помечен «подозрительный», оно не исполняется"
        )
    p.add_event(
        p.event(
            kind="действие",
            name="strateg fetch",
            idempotency_hash=h,
            status=status,
            duration_s=round(time.monotonic() - started, 2) if status == "выполнено" else None,
            message=note if status != "выполнено" else None,
        )
    )
    p.save_state()
    return source


def _analyse(
    page: fetchlib.Page, extra: dict[str, Any]
) -> tuple[str, str | None, str, tuple[safety.SafetyReport, str, str] | None]:
    """Ответ -> (доступ, пояснение, вид, (отчёт проверок, текст снимка, кодировка))."""
    kind = "PDF" if fetchlib.is_pdf(page) else "страница"
    if page.status >= 400 and page.status not in (401, 403, 429):
        return "источник недоступен: ошибка", f"ответ {page.status}", kind, None
    if page.status >= 300 and page.status not in (401, 403, 429):
        return "источник недоступен: ошибка", f"ответ {page.status} без адреса перенаправления", kind, None
    if kind == "PDF":
        try:
            raw_text = fetchlib.extract_pdf(page.body)
        except fetchlib.FetchProblem as exc:
            return exc.access, exc.note, kind, None
        encoding, html = "utf-8", ""
    else:
        html, encoding = fetchlib.decode_body(page.body, page.content_type)
        raw_text = fetchlib.extract_html(html) if page.status < 400 else ""
    if fetchlib.looks_blocked(page.status, html, raw_text):
        return (
            "источник недоступен: защита",
            "сайт отвечает проверкой или запретом для программ; обход не выполняется",
            kind,
            None,
        )
    if len(raw_text.strip()) < fetchlib.MIN_TEXT_CHARS:
        return (
            "нужен браузер",
            "в ответе нет текста (страница строится скриптом или PDF без текстового слоя); "
            "сохраните страницу вручную в папку inputs/",
            kind,
            None,
        )
    rep = safety.inspect_text(raw_text)
    if rep.closed_markers:
        extra["closed_markers"] = rep.closed_markers
        return (
            "источник недоступен: ошибка",
            "найдены признаки закрытых данных ("
            + ", ".join(rep.closed_markers)
            + "): страница не сохранена. "
            "Если материал общедоступен, сохраните его вручную в папку inputs/ и подтвердите это",
            kind,
            None,
        )
    return "получен", None, kind, (rep, rep.text, encoding)


def _snapshot_path(p: Project, s: SourceModel) -> str | None:
    """Путь к снимку для Read: от корня репозитория, а если проект вне репозитория, то полный."""
    if not s.text_path:
        return None
    full = (p.root / s.text_path).resolve()
    from strateg.cli import find_repo_root

    repo = find_repo_root()
    if repo is not None:
        try:
            return full.relative_to(repo.resolve()).as_posix()
        except ValueError:
            pass
    return str(full)


def _fetch_report(
    args: argparse.Namespace,
    s: SourceModel,
    p: Project,
    *,
    cached: bool,
    warnings: list[str],
    extra: dict[str, Any],
) -> int:
    closed = s.closed_markers or extra.get("closed_markers", [])
    data = {
        "source": s.id,
        "url": s.url,
        "final_url": s.fetch.final_url if s.fetch else None,
        "region": s.fetch.region if s.fetch else None,
        "access": s.access,
        "cached": cached,
        "kind": s.kind,
        "snapshot": s.text_path,
        "snapshot_path": _snapshot_path(p, s),
        "sha256": s.text_sha256,
        "suspicious": s.suspicious,
        "closed_markers": closed,
        "warnings": warnings,
    }
    marks = []
    if s.suspicious:
        marks.append("подозрительный")
    if closed:
        marks.append("признаки закрытых данных: " + ", ".join(closed))
    if s.access == "получен":
        lines = [
            f"Источник {s.id}: " + ("снимок уже сохранён" if cached else "снимок сохранён") + ".",
            f"Снимок: {data['snapshot_path']}",
            f"Адрес: {data['final_url'] or s.url}",
            f"Регион: {data['region'] or 'не указан'}",
            f"Доступ: {s.access}",
            "Пометки: " + ("; ".join(marks) if marks else "нет"),
        ]
        lines += [f"Внимание: {w}" for w in warnings]
        return emit(args, data, lines)
    data["note"] = s.access_note
    err = CliError(f"{s.id}: {s.access}. {s.access_note}", warnings)
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, **data, "error": err.message}, ensure_ascii=False))
        return 1
    return fail(args, err)


# ---------------------------------------------------------------------------
# claim add / list
# ---------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuoteIn(_In):
    source: SourceId
    text: Text
    locator: Text | None = None
    publisher: Text | None = None
    publisher_kind: PublisherKind | None = None
    self_reported: bool | None = None


class ClaimIn(_In):
    text: Text
    type: ClaimType
    subject: Text
    origin: Origin | None = None
    period: Text | None = None
    region: Text | None = None
    basis: list[ClaimId] = Field(default_factory=list)
    check_method: Text | None = None
    forecast_range: ForecastRange | None = None
    taken_from: Text | None = None
    quotes: list[QuoteIn] = Field(default_factory=list)
    plan_question: Text | None = None
    step: Step | None = None


def _format(prefix: str, exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = list(err["loc"])
        where = f", поле {_where(loc)}" if loc else ""
        out.extend(f"{prefix}{where}: {m}" for m in _explain(err))
    return out


def _read_input(path: str) -> Any:
    if path == "-":
        raw = sys.stdin.buffer.read()
    else:
        f = Path(path)
        if not f.is_file():
            raise CliError(f"файл {path} не найден")
        raw = f.read_bytes()
    try:
        return yaml.safe_load(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise CliError("файл с утверждениями не в кодировке UTF-8") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (строка {mark.line + 1})" if mark else ""
        raise CliError(f"файл с утверждениями не читается как YAML или JSON{where}") from exc


def claim_key(text: str, kind: str, quotes: list[tuple[str, str]]) -> str:
    """Ключ идемпотентности `claim add`: хеш текста, источника и цитаты (и типа)."""
    return proj.key_hash("claim", norm(text), kind, sorted((s, normalize_quote(q)) for s, q in quotes))


def _default_origin(c: ClaimIn) -> str:
    if c.origin:
        return c.origin
    if c.type == "синтетика":
        return "синтетика"
    return "источник" if c.type == "факт" or c.quotes else "агент"


def _build_quotes(p: Project, c: ClaimIn, label: str, errors: list[str]) -> list[Quote]:
    by_id = {s.id: s for s in p.sources.sources}
    out: list[Quote] = []
    for j, q in enumerate(c.quotes, start=1):
        where = f"{label}, цитата {j}"
        src = by_id.get(q.source)
        if src is None:
            errors.append(f"{where}: источника {q.source} нет в реестре; сначала `strateg fetch`")
            continue
        if src.closed_markers and not src.closed_confirmed:
            errors.append(
                f"{where}: в источнике {src.id} найдены признаки закрытых данных, "
                "человек не подтвердил, что он общедоступный"
            )
            continue
        if src.text_path is None or src.text_sha256 is None:
            errors.append(f"{where}: у источника {src.id} нет снимка текста (доступ: «{src.access}»)")
            continue
        if c.type == "факт" and src.kind == "синтетика":
            errors.append(f"{where}: источник {src.id} — синтетика, фактом она не становится")
            continue
        path = p.root / src.text_path
        if not path.is_file() or proj.sha256_bytes(path.read_bytes()) != src.text_sha256:
            errors.append(f"{where}: снимок {src.text_path} изменён или не найден; загрузите источник заново")
            continue
        text = path.read_bytes().decode("utf-8")
        found = quotelib.check_quote(q.text, text)
        if found is None:
            errors.append(
                f"{where}: цитата не найдена в снимке {src.id}: {quotelib.explain_miss(q.text, text)}"
            )
            continue
        locator = q.locator
        if src.kind in LOCATOR_KINDS and locator is None:
            page = quotelib.find_page(q.text, text) if src.kind == "PDF" else None
            if page is None:
                errors.append(f"{where}: для источника вида «{src.kind}» нужно место цитаты (locator)")
                continue
            locator = f"стр. {page}"
        if (
            c.type == "факт"
            and src.fetch
            and src.fetch.region
            and norm(c.region or "") != norm(src.fetch.region)
        ):
            errors.append(
                f"{label}: у источника {src.id} регион «{src.fetch.region}», "
                f"а у факта «{c.region or 'не указан'}»: "
                f"укажите region: {src.fetch.region}"
            )
            continue
        out.append(
            Quote(
                source=q.source,
                text=q.text,
                locator=locator,
                publisher=q.publisher,
                publisher_kind=q.publisher_kind,
                self_reported=q.self_reported,
                check=QuoteCheck(
                    method=found.method,  # type: ignore[arg-type]
                    similarity=found.similarity,
                    snapshot_sha256=src.text_sha256,
                    checked_at=proj.now(),
                ),
            )
        )
    return out


def cmd_claim_add(args: argparse.Namespace) -> int:
    p = _open(args)
    raw = _read_input(args.file)
    if isinstance(raw, dict) and isinstance(raw.get("claims"), list):
        raw = raw["claims"]
    items = raw if isinstance(raw, list) else [raw]
    if not items or not all(isinstance(i, dict) for i in items):
        raise CliError("в файле нужно утверждение (набор полей «ключ: значение») или список утверждений")

    errors: list[str] = []
    prepared: list[tuple[ClaimIn, str]] = []
    for n, item in enumerate(items, start=1):
        label = f"утверждение №{n}"
        try:
            prepared.append((ClaimIn.model_validate(item), label))
        except ValidationError as exc:
            errors.extend(_format(label, exc))
    if errors:
        raise CliError("утверждения не записаны: файл не по схеме", errors)

    started = time.monotonic()
    known = {claim_key(c.text, c.type, [(q.source, q.text) for q in c.quotes]): c.id for c in p.claims.claims}
    results: list[dict[str, Any]] = []
    new_claims: list[tuple[Claim, str]] = []
    batch_keys: dict[str, str] = {}
    next_no = max((int(c.id.split("-")[1]) for c in p.claims.claims), default=0) + 1
    suspicious_sources = {s.id for s in p.sources.sources if s.suspicious}
    attempt = max(_attempts(p, p.state.step), 1)
    for c, label in prepared:
        key = claim_key(c.text, c.type, [(q.source, q.text) for q in c.quotes])
        if key in known:
            results.append({"id": known[key], "duplicate": True, "type": c.type})
            continue
        if key in batch_keys:
            results.append({"id": batch_keys[key], "duplicate": True, "type": c.type})
            continue
        before = len(errors)
        known_ids = {x.id for x in p.claims.claims} | set(batch_keys.values())
        for ref in c.basis:
            if ref not in known_ids:
                errors.append(f"{label}, поле basis: утверждения {ref} нет в реестре")
        quotes = _build_quotes(p, c, label, errors)
        if len(errors) > before:
            continue
        cid = f"У-{next_no}"
        try:
            claim = Claim(
                id=cid,
                text=c.text,
                type=c.type,
                origin=_default_origin(c),  # type: ignore[arg-type]
                subject=c.subject,
                period=c.period,
                region=c.region,
                basis=c.basis,
                check_method=c.check_method,
                forecast_range=c.forecast_range,
                taken_from=c.taken_from,
                quotes=quotes,
                suspicious=any(q.source in suspicious_sources for q in quotes),
                status="действует",
                status_history=[StatusChange(status="действует", at=proj.now(), by="агент")],
                plan_question=c.plan_question,
                step=c.step or p.state.step,
                attempt=attempt,
            )
        except ValidationError as exc:
            errors.extend(_format(label, exc))
            continue
        next_no += 1
        batch_keys[key] = cid
        new_claims.append((claim, key))
        results.append({"id": cid, "duplicate": False, "type": c.type, "suspicious": claim.suspicious})
    if errors:
        raise CliError("утверждения не записаны", errors)

    if new_claims:
        with proj.transaction(p.root):
            for claim, key in new_claims:
                p.claims.claims.append(claim)
                p.add_event(
                    p.event(
                        step=claim.step,
                        kind="действие",
                        name="strateg claim add",
                        idempotency_hash=key,
                        status="выполнено",
                        duration_s=round(time.monotonic() - started, 2),
                        message=claim.id,
                    )
                )
            p.save_claims()
            p.save_state()
    lines = []
    for r in results:
        lines.append(
            f"{r['id']}: уже записано ({r['type']}), дубль не создан"
            if r["duplicate"]
            else f"{r['id']}: записано ({r['type']})"
        )
        if r.get("suspicious"):
            lines.append(f"  внимание: {r['id']} помечено «подозрительный» (источник с обращением к ИИ)")
    return emit(args, {"claims": results}, lines)


def cmd_claim_list(args: argparse.Namespace) -> int:
    p = _open(args)
    claims = [c for c in p.claims.claims if not args.type or c.type == args.type]
    data = [
        {
            "id": c.id,
            "type": c.type,
            "status": c.status,
            "origin": c.origin,
            "text": c.text,
            "subject": c.subject,
            "period": c.period,
            "region": c.region,
            "sources": sorted({q.source for q in c.quotes}),
            "suspicious": c.suspicious,
            "step": c.step,
        }
        for c in claims
    ]
    lines = [
        f"{d['id']} [{d['type']}, {d['status']}] {d['text']}"
        + (f" (источники: {', '.join(d['sources'])})" if d["sources"] else "")
        for d in data
    ] or ["Утверждений нет."]
    return emit(args, {"claims": data}, lines)


# ---------------------------------------------------------------------------
# check, cost, status
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    root = proj.resolve_project(getattr(args, "project", None))
    errors = check_project(root)
    if errors:
        return fail(args, CliError(f"проверка проекта {root.name}: ошибок {len(errors)}", errors))
    return emit(args, {"project": root.name, "errors": []}, [f"Проект {root.name} в порядке: ошибок нет."])


def cmd_cost_add(args: argparse.Namespace) -> int:
    p = _open(args)
    if args.usd is None and args.source:
        raise UsageError("--source задаётся вместе с --usd")
    if args.usd is not None and args.usd < 0:
        raise UsageError("--usd не может быть отрицательным")
    tokens = {
        "input_tokens": args.input,
        "output_tokens": args.output,
        "cache_read_tokens": args.cache_read,
        "cache_write_5m_tokens": args.cache_write_5m,
        "cache_write_1h_tokens": args.cache_write_1h,
    }
    if any(v < 0 for v in tokens.values()):
        raise UsageError("число токенов не может быть отрицательным")
    entry = "итог сессии" if args.total else "по сообщению"
    message_id = None
    if not args.total:
        message_id = (
            args.message_id
            or "авто-" + proj.key_hash("cost", args.session, args.role, args.model, args.usd, tokens)[:16]
        )
        if any(r.session_id == args.session and r.message_id == message_id for r in p.costs):
            return emit(
                args,
                {"duplicate": True, "message_id": message_id},
                ["Эта запись уже учтена, повтор не добавлен."],
            )
    record = CostRecord(
        schema_version=SCHEMA_VERSION,
        time=proj.now(),
        session_id=args.session,
        role=args.role,
        model=args.model,
        environment=args.environment,
        step=p.state.step,
        entry=entry,  # type: ignore[arg-type]
        message_id=message_id,
        cost_usd=args.usd,
        cost_source=(args.source or "оценка") if args.usd is not None else "нет данных",  # type: ignore[arg-type]
        **tokens,
    )
    with proj.transaction(p.root):
        p.add_cost(record)
        _refresh_budget(p)
        p.save_state()
    total, precision, complete = proj.cost_total(p.costs)
    line = "Стоимость учтена" + (
        f": всего {_usd(total)} ({precision})" if total is not None else ": данных о цене пока нет"
    )
    return emit(
        args,
        {
            "entry": entry,
            "message_id": message_id,
            "total_usd": total,
            "precision": precision,
            "complete": complete,
        },
        [line + ("" if complete or total is None else "; часть записей без цены")],
    )


def _status_data(p: Project) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for s in p.sources.sources:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    active = [c for c in p.claims.claims if c.status == "действует"]
    by_type: dict[str, int] = {}
    for c in active:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    without_quote = sum(
        1
        for c in active
        if (c.type == "факт" and not any(q.check for q in c.quotes))
        or (c.type in ("гипотеза", "прогноз") and not (c.basis or c.quotes))
    )
    total_usd, precision, complete = proj.cost_total(p.costs)
    open_steps = [e.step for e in _open_steps(p).values()]
    waiting = [g for g in GATES if p.state.gates.get(g) == "ждут ответа"]  # type: ignore[call-overload]
    if waiting:
        nxt = (
            f"ждём ответа на {waiting[0]}: следующее сообщение человека и есть ответ; "
            f'запишите его командой `strateg gate answer {waiting[0]} --answer "<ответ>"`'
        )
    else:
        nxt = p.state.next_action
    return {
        "project": p.id,
        "mode": p.state.mode,
        "step": p.state.step,
        "step_name": STEP_NAMES[p.state.step],
        "step_in_progress": p.state.step in open_steps,
        "waiting_gates": waiting,
        "gates": dict(p.state.gates),
        "sources": {
            "total": len(p.sources.sources),
            "by_kind": by_kind,
            "unavailable": sum(1 for s in p.sources.sources if s.access != "получен"),
            "suspicious": [s.id for s in p.sources.sources if s.suspicious],
            "closed_unconfirmed": [
                s.id for s in p.sources.sources if s.closed_markers and not s.closed_confirmed
            ],
        },
        "claims": {
            "active": len(active),
            "total": len(p.claims.claims),
            "by_type": by_type,
            "without_quote": without_quote,
        },
        "failures": sum(1 for e in p.journal if e.kind == "действие" and e.status == "ошибка"),
        "work_minutes": round(proj.work_seconds(p.journal) / 60, 2),
        "limit_minutes": p.state.budget.limit_minutes,
        "cost_usd": total_usd,
        "cost_precision": precision,
        "cost_complete": complete,
        "limit_usd": p.state.budget.limit_usd,
        "next_action": nxt,
    }


def status_line(d: dict[str, Any]) -> str:
    """Одна строка статуса из `docs/user-journey.md`: «Шаг 2б из 8 · Сбор · источников 9 · …»."""
    parts = [f"Шаг {d['step']} из 8 · {d['step_name']}"]
    parts.append(f"источников {d['sources']['total']}")
    parts.append(f"утверждений {d['claims']['active']}, без цитаты {d['claims']['without_quote']}")
    parts.append(f"сбоев {d['failures']}")
    cost, limit = d["cost_usd"], d["limit_usd"]
    if cost is not None and limit is not None:
        parts.append(f"{_usd(cost)} из ${limit:g}".replace(".", ","))
    elif cost is not None:
        parts.append(_usd(cost))
    elif limit is not None:
        parts.append("стоимость: нет данных")
    parts.append(f"{round(d['work_minutes'])} из {d['limit_minutes']:g} мин")
    return " · ".join(parts)


def cmd_status(args: argparse.Namespace) -> int:
    p = _open(args)
    d = _status_data(p)
    d["line"] = status_line(d)
    if args.line:
        return emit(args, {"line": d["line"]}, [d["line"]])
    cost = (
        "нет данных"
        if d["cost_usd"] is None
        else f"{_usd(d['cost_usd'])} ({d['cost_precision']}{'' if d['cost_complete'] else ', неполно'})"
    )
    kinds = ", ".join(f"{k} {v}" for k, v in d["sources"]["by_kind"].items())
    types = ", ".join(f"{k} {v}" for k, v in d["claims"]["by_type"].items())
    lines = [
        f"Проект: {d['project']} · режим: {d['mode']}",
        f"Шаг: {d['step']} ({d['step_name']})" + (" · в работе" if d["step_in_progress"] else ""),
        "Ворота: " + ", ".join(f"{g} {s}" for g, s in d["gates"].items()),
        f"Источники: {d['sources']['total']}"
        + (f" ({kinds})" if kinds else "")
        + (f", недоступны: {d['sources']['unavailable']}" if d["sources"]["unavailable"] else ""),
        f"Утверждения: {d['claims']['active']} действуют" + (f" ({types})" if types else ""),
        f"Время работы: {_num(d['work_minutes'])} из {d['limit_minutes']:g} мин · стоимость: {cost}",
    ]
    if d["sources"]["suspicious"]:
        lines.append(
            "Внимание: источники с пометкой «подозрительный»: " + ", ".join(d["sources"]["suspicious"])
        )
    if d["sources"]["closed_unconfirmed"]:
        lines.append(
            "Внимание: признаки закрытых данных не подтверждены человеком: "
            + ", ".join(d["sources"]["closed_unconfirmed"])
        )
    if d["waiting_gates"]:
        lines.insert(
            0, "Ждём ответа на " + ", ".join(d["waiting_gates"]) + ": следующее сообщение человека — ответ"
        )
    lines.append(f"Дальше: {d['next_action'] or 'не задано'}")
    return emit(args, d, lines)
