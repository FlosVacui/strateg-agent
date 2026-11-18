"""Команды CLI, которые меняют и показывают состояние проекта (ADR-0002).

Каждая команда: открыть проект, внутри `transaction` изменить файлы только через модели, вернуть код
(0 — успех, 1 — ошибка данных или правил, 2 — ошибка использования). Сообщения — по-русски, с
`--json` — машинный вывод.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from strateg import __version__, materials, outputs, project_copy, safety
from strateg import budget as budgetlib
from strateg import calc as calclib
from strateg import fetch as fetchlib
from strateg import pack as packlib
from strateg import project as proj
from strateg import quote as quotelib
from strateg import release as releaselib
from strateg.hooks import flags as hookflags
from strateg.models import (
    LOCATOR_KINDS,
    SCHEMA_VERSION,
    CalcRef,
    Claim,
    ClaimId,
    ClaimType,
    CostRecord,
    Dispute,
    FetchInfo,
    ForecastRange,
    FrameBody,
    FrameFile,
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
    latin_hint,
    norm,
)
from strateg.models import Source as SourceModel
from strateg.project import CliError, NoActiveProject, Project, UsageError
from strateg.project_check import (
    _PLAN_LIKE,
    _PLAN_REF,
    FRAME_FILE,
    STALE_REF_NOTE,
    _explain,
    _where,
    check_project,
    frame_key,
    synthetic_claims,
)

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


def _cmd_new_copy(args: argparse.Namespace) -> int:
    if args.mode is not None or args.region is not None:
        raise UsageError("с --from режим и регион берутся из источника: --mode и --region не нужны")
    if not args.source.strip():
        raise UsageError(
            "не указан проект-источник: после --from нужен номер проекта или папка, например example"
        )
    done = project_copy.copy_project(args.source, args.id)
    p = done.project
    d = _status_data(p)
    notes = (
        [f"В копию не вошли {', '.join(done.skipped)}: это временные файлы и кэш источника."]
        if done.skipped
        else []
    )
    if done.mentions:
        notes.append(
            f"В файлах копии есть ссылки на папку источника `projects/{done.source}/`: "
            + ", ".join(done.mentions)
        )
    data = {
        "project": p.id,
        "from": done.source,
        "mode": p.state.mode,
        "step": p.state.step,
        "gates": dict(p.state.gates),
        "path": str(p.root),
        "active": True,
        "files": done.files,
        "not_copied": done.skipped,
        "mentions": done.mentions,
        "next_action": d["next_action"],
    }
    return emit(
        args,
        data,
        [
            f"Проект {p.id} создан как копия «{done.source}» и выбран активным (режим: {p.state.mode}).",
            f"Папка: {p.root}",
            f"Источник остановлен на шаге {p.state.step} ({d['step_name']}); ворота: "
            + ", ".join(f"{g} {s}" for g, s in p.state.gates.items())
            + ".",
            "Время и стоимость копии считаются с нуля: работа над источником в них не входит.",
            *notes,
            "Дальше: `strateg status`, затем " + (d["next_action"] or "продолжить с текущего шага") + ".",
        ],
    )


def cmd_new(args: argparse.Namespace) -> int:
    if getattr(args, "source", None) is not None:
        return _cmd_new_copy(args)
    p = proj.create_project(args.id, args.mode or "автопилот", args.region)
    proj.set_active(args.id)
    errors = check_project(p.root)
    if errors:
        return fail(args, CliError("созданный проект не прошёл проверку", errors))
    return emit(
        args,
        {"project": p.id, "mode": p.state.mode, "path": str(p.root), "active": True},
        [
            f"Проект {p.id} создан и выбран активным (режим: {p.state.mode}).",
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


def _attempts(p: Project, step: str, *, whole: bool = False) -> int:
    """Сколько раз шаг начат в этом проекте (у копии — после «копии проекта»); `whole` — по всему журналу."""
    name = proj.step_action_name(step)
    events = p.journal if whole else proj.own_events(p.journal)
    return sum(1 for e in events if e.kind == "действие" and e.name == name and e.status == "начато")


def _refresh_budget(p: Project) -> None:
    p.state.budget.spent_minutes = round(proj.work_seconds(p.journal) / 60, 2)
    p.state.budget.spent_usd = proj.cost_total(p.costs)[0]


def cmd_step_start(args: argparse.Namespace) -> int:
    p = _open(args)
    step = _norm_step(args.step)
    notes: list[str] = []
    with proj.transaction(p):
        open_steps = _open_steps(p)
        # длительность считается по журналу до записи «прервано»
        worked = {h: proj.interrupted_seconds(p.journal, e) for h, e in open_steps.items()}
        for h, e in open_steps.items():
            p.add_event(
                p.event(
                    step=e.step,
                    kind="действие",
                    name=e.name,
                    idempotency_hash=h,
                    status="прервано",
                    duration_s=worked[h],
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
                # ключ не повторяет ключ источника: журнал копии начинается с его событий
                idempotency_hash=proj.key_hash("step", step, _attempts(p, step, whole=True) + 1),
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
    release_no = 0
    with proj.transaction(p):
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
        begin = proj.own_start(p.journal, start.time)  # шаг, начатый до копии проекта, считается от копии
        total = max((end - begin).total_seconds(), 0.0)
        waited = proj.waited_seconds(begin, end, proj.human_waits(p.journal))
        duration = round(max(total - waited, 0.0), 1)
        message = None
        if step == "7":  # выпуск: снимок файлов выхода и документов, с ним потом сравнивает `strateg diff`
            release_no, kept = releaselib.take_snapshot(p)
            if release_no:
                message = f"снимок выпуска {release_no}: файлов {len(kept)}, history/release-{release_no}/"
        p.add_event(
            p.event(
                time=end,
                step=step,
                kind="действие",
                name=name,
                idempotency_hash=last_hash,
                status="выполнено",
                duration_s=duration,
                message=message,
            )
        )
        p.state.step = step
        p.state.next_action = args.next or f"Шаг {step} завершён; начать следующий шаг"
        _refresh_budget(p)
        p.save_state()
    lines = [f"Шаг {step} ({STEP_NAMES[step]}) выполнен за {_num(duration / 60)} мин (без ожидания ответов)."]
    if release_no:
        lines.append(
            f"Снимок выпуска {release_no}: history/release-{release_no}/ (с ним сравнивает `strateg diff`)."
        )
    data = {"step": step, "duration_s": duration, "waited_s": round(waited, 1)}
    if release_no:
        data["release"] = release_no
    return emit(args, data, lines)


def cmd_gate_open(args: argparse.Namespace) -> int:
    p = _open(args)
    gate = _norm_gate(args.gate)
    if args.questions < 0:
        raise UsageError("число вопросов не может быть отрицательным")
    status = "пройдены автоматически" if args.questions == 0 else "ждут ответа"
    with proj.transaction(p):
        if p.state.gates[gate] == "ждут ответа":  # type: ignore[index]
            return emit(
                args,
                {"gate": gate, "status": "ждут ответа", "already_open": True},
                [f"Ворота {gate} уже открыты."],
            )
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
    with proj.transaction(p):
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
        frame = _current_frame(p) if gate == "В1" else None  # рамка, на которую отвечает человек
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
                    frame=frame,
                ),
            )
        )
        p.state.gates[gate] = "отвечено"  # type: ignore[index]
        p.save_state()
    return emit(args, {"gate": gate, "status": "отвечено"}, [f"Ответ на ворота {gate} записан."])


def _current_frame(p: Project) -> FrameBody:
    """Рамка из `frame.yaml` для события ответа на В1; рамки нет или она не по схеме — отказ."""
    path = p.root / FRAME_FILE
    if not path.is_file():
        raise CliError(
            f"рамки нет: ответ на В1 хранит рамку, а {FRAME_FILE} не записан; "
            "запишите черновик рамки командой `strateg write --file <файл>` (вид frame) и повторите"
        )
    try:
        frame = FrameFile.model_validate(proj.read_yaml(path), by_name=False)
    except ValidationError as exc:
        raise CliError(f"{FRAME_FILE} не по схеме, ответ на В1 не записан", _format(FRAME_FILE, exc)) from exc
    return FrameBody.model_validate(frame_key(frame))


def cmd_mode(args: argparse.Namespace) -> int:
    p = _open(args)
    mode = args.mode.strip()
    with proj.transaction(p):
        if p.state.mode == mode:
            return emit(args, {"mode": mode, "changed": False}, [f"Режим уже «{mode}»."])
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
    with proj.transaction(p):
        stops = sum(1 for e in proj.own_events(p.journal) if proj.is_stop(e))
        earlier = sum(1 for e in p.journal if proj.is_stop(e))  # ключ не повторяет ключ остановки источника
        p.add_event(
            p.event(
                kind="действие",
                name="внеплановая остановка",
                idempotency_hash=proj.key_hash("stop", earlier + 1),
                status="выполнено",
                duration_s=0,
                message=reason,
            )
        )
        # Вопрос человеку не теряется и в новом чате: его показывает `status`.
        p.state.next_action = f"Внеплановая остановка: {reason}. Нужен ответ человека"
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


def _orphan_snapshot_number(p: Project, text: str) -> int | None:
    """Номер снимка без записи, в котором лежит этот же текст: запись о нём потеряна (параллельной командой).

    Новая запись встаёт на тот же номер и тот же файл, и `check` перестаёт видеть лишний снимок.
    Берётся наименьший такой номер, которого нет в реестре.
    """
    folder = p.root / "sources"
    if not folder.is_dir():
        return None
    known = {s.id for s in p.sources.sources} | {s.text_path for s in p.sources.sources if s.text_path}
    data = text.replace("\r\n", "\n").encode("utf-8")  # как пишет `write_text`
    found = []
    for path in folder.iterdir():
        m = re.fullmatch(r"i-([1-9][0-9]*)\.txt", path.name)
        if m and f"sources/{path.name}" not in known and f"И-{m[1]}" not in known and path.is_file():
            if path.read_bytes() == data:
                found.append(int(m[1]))
    return min(found, default=None)


def _same_address(s: SourceModel, url: str, region: str | None) -> bool:
    """Источник связан с адресом и регионом: основным адресом, дополнительным или итоговым после переходов."""
    if s.url is None or (s.fetch.region if s.fetch else None) != region:
        return False
    final = s.fetch.final_url if s.fetch and s.access == "получен" else None
    return url == s.url or url in (s.other_urls or []) or url == final


def _known_for_address(
    p: Project, url: str, region: str | None
) -> tuple[SourceModel | None, SourceModel | None]:
    """Прежний источник адреса при любом `access`: (последний полученный снимок, недоступная запись).

    Из цепочки перепроверок берётся последняя запись: на неё никто не ссылается через `refreshes`.
    Второе значение есть только когда полученного снимка нет: такую запись повторная загрузка обновит.
    """
    matches = [s for s in p.sources.sources if _same_address(s, url, region)]
    refreshed = {s.refreshes for s in p.sources.sources if s.refreshes}
    heads = [s for s in matches if s.id not in refreshed] or matches
    got_heads = [s for s in heads if s.access == "получен"]
    tips = [s for s in got_heads if s.refreshes]  # конец цепочки перепроверок новее старых дублей
    if got_heads:
        return (tips[-1] if tips else got_heads[0]), None
    return None, (matches[0] if matches else None)


def _refresh_key(p: Project, url: str, region: str | None) -> str:
    """Ключ перепроверки: у каждой законченной перепроверки свой номер, иначе журнал счёл бы её повтором.

    Начатая и не законченная (или неудачная) перепроверка ключ сохраняет: повтор — та же попытка.
    """
    n = 1
    while True:
        h = proj.key_hash("fetch", url, region, "refresh", n)
        done = any(
            e.kind == "действие" and e.idempotency_hash == h and e.status == "выполнено" for e in p.journal
        )
        if not done:
            return h
        n += 1


def _fetch_done(p: Project, h: str) -> bool:
    """В журнале уже есть «выполнено» с этим ключом: такую загрузку закончила другая команда."""
    return any(
        e.kind == "действие" and e.idempotency_hash == h and e.status == "выполнено" for e in p.journal
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    """Загрузка страницы в три приёма (ADR-0002, «Параллельные команды»).

    1. Под блокировкой папки: адрес уже записан? иначе ключ загрузки и событие «начато».
    2. Без блокировки: сеть и разбор страницы. Параллельные исследователи не ждут друг друга.
    3. Под блокировкой: проект перечитан заново, повтор адреса и текста проверен ещё раз, номер `И-n`,
       снимок, запись источника и события. Файл снимка пишется только здесь, лишнего не остаётся.
    """
    p = _open(args)
    try:
        url = fetchlib.normalize_url(args.url)
    except fetchlib.AddressRefused as exc:
        raise UsageError(str(exc)) from exc
    region = args.region or p.state.region
    refresh = bool(getattr(args, "refresh", False))
    got, _retry = _known_for_address(p, url, region)
    if got is not None and not refresh:  # источники не пропадают: прежний ответ верен и без блокировки
        return _fetch_report(args, got, p, cached=True, warnings=[], extra={})
    warnings: list[str] = []
    extra: dict[str, Any] = {}
    started = time.monotonic()

    with proj.transaction(p, rollback=False):
        got, _retry = _known_for_address(p, url, region)
        if got is not None and not refresh:  # пока ждали блокировку, адрес записала другая команда
            return _fetch_report(args, got, p, cached=True, warnings=[], extra={})
        # `--refresh` без прежнего снимка — обычная загрузка; недоступная прежде запись обновляется на месте
        prior = got if refresh else None
        if refresh and prior is None:
            warnings.append(
                "прежнего снимка нет, загружено как обычный fetch (адрес и регион те же, что в запросе)"
            )
        h = _refresh_key(p, url, region) if prior is not None else proj.key_hash("fetch", url, region)
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

    # Сеть и разбор страницы: папка проекта свободна.
    access, note, final_url, page = "получен", None, None, None
    refused: str | None = None
    try:
        fetcher = make_fetcher(p.root / proj.CACHE_DIR / "fetch.json")
        page = fetcher.get(url)
        final_url = page.url
    except (fetchlib.AddressRefused, fetchlib.PinningUnavailable) as exc:
        refused = str(exc)
    except fetchlib.RobotsDenied as exc:
        access = "запрещено robots.txt"
        if exc.unavailable:
            note = (
                "robots.txt недоступен (ошибка сервера или нет связи): по RFC 9309 загрузка запрещена; "
                "повторите позже или сохраните страницу вручную в папку inputs/ проекта"
            )
        else:
            note = (
                f"robots.txt сайта запрещает загрузку {exc.url} для strateg; сохраните страницу вручную "
                "в папку inputs/ проекта и добавьте её как материал"
            )
    except fetchlib.FetchProblem as exc:
        access, note = exc.access, exc.note
    kind: str = "страница"
    snapshot: tuple[safety.SafetyReport, str, str] | None = None
    if page is not None and access == "получен":
        access, note, kind, snapshot = _analyse(page, extra)

    with proj.transaction(p, rollback=False):
        known, retry = _known_for_address(p, url, region)
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
        elif known is not None and (not refresh or _fetch_done(p, h)):
            # Пока шла загрузка, этот адрес записала другая команда: второго источника и второй отметки
            # «выполнено» с тем же ключом нет, отдаём прежний номер.
            _finish_event(
                p,
                h,
                started,
                "пропущено",
                f"{known.id}: адрес записан параллельной командой, повтор не записан",
            )
            p.save_state()
            if refresh:
                extra["parallel"] = True
            return _fetch_report(
                args,
                known,
                p,
                cached=True,
                warnings=[f"адрес записала параллельная команда: {known.id}"],
                extra=extra,
            )
        else:
            prior = known if refresh else None
            source = _store_result(
                p,
                h,
                started,
                url,
                region,
                retry,
                prior,
                access,
                note,
                final_url,
                page,
                kind,
                snapshot,
                warnings,
                extra,
            )
    if refused is not None:
        raise CliError(refused)
    # Папку проверяют без блокировки (она держится только на запись): ошибки перепроверяются под ней, чтобы
    # не принять за ошибку то, что соседняя команда пишет прямо сейчас.
    found = check_project(p.root)
    if found:
        with proj.locked(p.root):
            found = check_project(p.root)
    for err in found:
        warnings.append("проверка проекта: " + err)
    return _fetch_report(args, source, p, cached=False, warnings=warnings, extra=extra)


def _finish_event(p: Project, h: str, started: float, status: str, message: str | None = None) -> None:
    """Итог загрузки в журнале: у «выполнено» есть длительность."""
    p.add_event(
        p.event(
            kind="действие",
            name="strateg fetch",
            idempotency_hash=h,
            status=status,  # type: ignore[arg-type]
            duration_s=round(time.monotonic() - started, 2) if status == "выполнено" else None,
            message=message,
        )
    )


def _chain_ids(p: Project, start: SourceModel, url: str, region: str | None) -> set[str]:
    """Источники этого адреса: его записи, `other_urls` и вся цепочка `refreshes` в обе стороны от `start`."""
    ids = {start.id} | {s.id for s in p.sources.sources if _same_address(s, url, region)}
    grown = True
    while grown:
        grown = False
        for s in p.sources.sources:
            linked = s.id in ids or (s.refreshes is not None and s.refreshes in ids)
            if linked and (s.id not in ids or (s.refreshes and s.refreshes not in ids)):
                ids.add(s.id)
                if s.refreshes:
                    ids.add(s.refreshes)
                grown = True
    return ids


def _text_twin(
    p: Project,
    text_sha: str | None,
    region: str | None,
    skip: SourceModel | None = None,
    within: set[str] | None = None,
) -> SourceModel | None:
    """Полученный веб-источник с тем же текстом снимка и тем же регионом (материалы `inputs/` не в счёт).

    `within` ограничивает поиск набором номеров (цепочка одного адреса при `--refresh`).
    """
    return next(
        (
            s
            for s in p.sources.sources
            if s is not skip
            and (within is None or s.id in within)
            and s.url is not None
            and s.text_sha256 == text_sha
            and s.access == "получен"
            and (s.fetch.region if s.fetch else None) == region
        ),
        None,
    )


def _standing_claims(p: Project, sid: str) -> list[str]:
    """Действующие утверждения с цитатой из источника."""
    return [
        c.id for c in p.claims.claims if c.status == "действует" and any(q.source == sid for q in c.quotes)
    ]


def _store_result(
    p: Project,
    h: str,
    started: float,
    url: str,
    region: str | None,
    retry: SourceModel | None,
    prior: SourceModel | None,
    access: str,
    note: str | None,
    final_url: str | None,
    page: fetchlib.Page | None,
    kind: str,
    snapshot: tuple[safety.SafetyReport, str, str] | None,
    warnings: list[str],
    extra: dict[str, Any],
) -> SourceModel:
    """Записывает итог загрузки: снимок, источник в реестре, события журнала. Зовут под блокировкой папки.

    Страница к этому моменту разобрана (`_analyse`: вид и текст снимка); здесь только сверка с реестром,
    номер и запись.

    - `retry` — недоступная прежде запись этого адреса: обновляется на месте, номер остаётся;
    - `prior` — полученный снимок при `--refresh`: тот же текст — отметка перепроверки у него, другой текст —
      новый источник со ссылкой `refreshes`, недоступная страница — запись не меняется;
    - текст нового адреса совпал со снимком другого источника — новый источник не создаётся, адрес
      добавляется к прежнему.
    """
    text_sha = proj.sha256_bytes(snapshot[1].encode("utf-8")) if snapshot is not None else None

    if prior is not None:
        if snapshot is None:
            extra["refresh_failed"] = f"{access}. {note}"
            extra["previous"] = prior.id
            failed = "пропущено" if access == "запрещено robots.txt" else "ошибка"
            _finish_event(p, h, started, failed, f"перепроверка {prior.id} не удалась: {access}")
            p.save_state()
            return prior
        if text_sha == prior.text_sha256:
            prior.rechecked_at = proj.now()
            p.save_sources()
            extra["rechecked"] = True
            _finish_event(p, h, started, "выполнено", f"{prior.id}: перепроверка, текст тот же")
            p.save_state()
            return prior
        back = _text_twin(p, text_sha, region, within=_chain_ids(p, prior, url, region))
        # страница вернулась к тексту другого снимка этого адреса: второго источника с ним не заводим
        if back is not None:
            back.rechecked_at = proj.now()
            p.save_sources()
            extra["rechecked"] = True
            extra["returned_to"] = back.id
            extra["previous"] = prior.id
            extra["claims_on_previous"] = _standing_claims(p, prior.id)
            message = (
                f"{back.id}: перепроверка {prior.id}, текст совпал со снимком {back.id}, нового источника нет"
            )
            _finish_event(p, h, started, "выполнено", message)
            p.save_state()
            return back
    elif snapshot is not None:
        twin = _text_twin(p, text_sha, region, skip=retry)
        if twin is not None and retry is None:
            others = list(twin.other_urls or [])
            if url != twin.url and url not in others:
                others.append(url)
            twin.other_urls = others or None
            p.save_sources()
            extra["same_text_as"] = twin.id
            message = f"текст совпадает с {twin.id} (другой адрес): новый источник не создан"
            _finish_event(p, h, started, "выполнено", message)
            p.save_state()
            return twin
        if twin is not None:  # недоступная прежде запись получила тот же текст: обновляем её на месте
            warnings.append(
                f"текст совпадает с {twin.id} (другой адрес): запись обновлена, снимок повторяет {twin.id}"
            )

    material = next(
        (
            s
            for s in p.sources.sources
            if s.url is None and s.access == "получен" and s.text_sha256 == text_sha
        ),
        None,
    )
    if snapshot is not None and material is not None:  # скачали то, что человек уже положил в inputs/
        extra["same_text_material"] = material.id
        warnings.append(
            f"текст совпадает с материалом {material.id} (из inputs/): запись материала не менялась"
        )

    if retry is not None:
        number = int(retry.id.split("-")[1])
    else:
        orphan = _orphan_snapshot_number(p, snapshot[1]) if snapshot is not None else None
        number = orphan if orphan is not None else proj.next_source_number(p)
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
    if prior is not None:
        data["refreshes"] = prior.id
    status = "ошибка" if access != "получен" else "выполнено"
    if access == "запрещено robots.txt":
        status = "пропущено"
    if snapshot is not None and page is not None:
        rep, text, encoding = snapshot
        rel = f"sources/i-{number}.txt"
        data_bytes = text.encode("utf-8")
        target = p.root / rel
        if not (
            target.is_file() and target.read_bytes() == text.replace("\r\n", "\n").encode("utf-8")
        ):  # тот же снимок на месте — не трогаем
            proj.write_text(target, text)
        info = FetchInfo(
            accessed_at=accessed,
            final_url=page.url,
            region=region,
            encoding=encoding,
            extractor=extra.get("extractor") or fetchlib.extractor_name(kind),
            raw_sha256=proj.sha256_bytes(page.body),
        )
        data.update(
            fetch=info,
            text_path=rel,
            text_sha256=proj.sha256_bytes(data_bytes),
            suspicious=bool(rep.instruction_signs),
            anonymized=rep.anonymized,
            closed_markers=rep.closed_markers,
        )
        if extra.get("warning"):
            warnings.append(extra["warning"])
        if rep.closed_markers:
            warnings.append(
                "найдены признаки закрытых данных ("
                + ", ".join(rep.closed_markers)
                + f"): снимок сохранён, но цитировать его нельзя, пока человек не подтвердит, что материал "
                f"общедоступный (`strateg source confirm {sid}`)"
            )
        if rep.anonymized:
            warnings.append("в тексте заменены на метки: " + ", ".join(rep.anonymized))
        if rep.key_kinds:
            warnings.append(safety.keys_message(rep.key_kinds))
        if extra.get("authors"):
            warnings.append(
                f"в снимке заменены авторы отзывов и комментариев: {extra['authors']} "
                f"(метки {fetchlib.AUTHOR_LABEL}, {fetchlib.MENTION_LABEL}); "
                "имя внутри текста отзыва разметкой не ловится"
            )
        if extra.get("review_names"):
            warnings.append(
                "в блоках отзывов возможны имена в свободном тексте (словарь ошибается: «Роза», «Вера»); "
                "имена не заменены, решаете вы: " + ", ".join(extra["review_names"])
            )
    else:
        data["fetch"] = info
    source = SourceModel.model_validate(data)
    if retry is not None:
        p.sources.sources[p.sources.sources.index(retry)] = source
    else:
        p.sources.sources.append(source)
    p.save_sources()
    if prior is not None:
        extra["refreshes"] = prior.id
        extra["claims_on_previous"] = _standing_claims(p, prior.id)
        if safety.AUTHORS_CATEGORY in source.anonymized and safety.AUTHORS_CATEGORY not in prior.anonymized:
            extra["prior_unmasked"] = prior.id
            warnings.append(safety.stale_snapshot_note(prior.id, source.id))
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
    if prior is not None:
        message: str | None = f"{sid}: перепроверка {prior.id}, текст изменился"
    else:
        message = note if status != "выполнено" else None
    _finish_event(p, h, started, status, message)
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
    visible = hidden = review_text = ""
    if kind == "PDF":
        try:
            raw_text = fetchlib.extract_pdf(page.body)
        except fetchlib.FetchProblem as exc:
            return exc.access, exc.note, kind, None
        encoding = "utf-8"
        extra["extractor"] = fetchlib.extractor_name(kind)
    else:
        html, encoding = fetchlib.decode_body(page.body, page.content_type)
        if page.content_type.lower().startswith("text/plain"):
            raw_text = html  # простой текст берётся как есть
            extra["extractor"] = f"plain-text {__version__}"
        elif page.status < 400:
            extraction = fetchlib.extract_page(html)
            raw_text = extraction.text
            extra["extractor"] = extraction.extractor
            if extraction.fallback:
                extra["warning"] = (
                    f"основной извлекатель потерял числа ({extraction.note}): "
                    "взят весь видимый текст страницы"
                )
            visible = fetchlib.page_title(html) + "\n" + fetchlib.visible_text(html)
            hidden = fetchlib.hidden_text(html)
            review_text = extraction.review_text
        else:
            raw_text = ""
    if fetchlib.looks_blocked(page.status, visible, raw_text):
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
    _mark_authors(rep, review_text, extra)
    # Скрытый текст в снимок не идёт, но обращение к ИИ в нём — повод пометить источник.
    for sign in safety.inspect_text(hidden).instruction_signs:
        if sign not in rep.instruction_signs:
            rep.instruction_signs.append(sign)
    return "получен", None, kind, (rep, rep.text, encoding)


def _mark_authors(rep: safety.SafetyReport, review_text: str, extra: dict[str, Any]) -> None:
    """Авторы отзывов, замененные в дереве страницы, и имена в свободном тексте отзывов, попавшем в снимок.

    Число считается по меткам в тексте снимка: блок, который извлекатель не взял, ничего не добавляет.
    Имена в свободном тексте разметкой не ловятся; словарь имён вызывается только если текст отзывов
    попал в снимок, и результат — предупреждение: заменяет человек.
    """
    authors = rep.text.count(fetchlib.AUTHOR_LABEL) + rep.text.count(fetchlib.MENTION_LABEL)
    if authors:
        rep.anonymized.append(safety.AUTHORS_CATEGORY)
        extra["authors"] = authors
    if not review_text:
        return
    flat = " ".join(rep.text.split())
    chunks = [" ".join(c.split()) for c in re.split(r"[.!?\n]+", review_text)]
    included = [c for c in chunks if len(c) >= 25 and c in flat]
    if included:
        names = [w for w, _ in safety.find_names("\n".join(included), limit=12)]
        if names:
            extra["review_names"] = names


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
        "extractor": s.fetch.extractor if s.fetch else None,
        "extraction_fallback": bool(
            s.fetch and s.fetch.extractor and s.fetch.extractor.startswith("lxml-visible-text")
        ),
        "suspicious": s.suspicious,
        "closed_markers": closed,
        "authors_masked": extra.get("authors", 0),
        "prior_unmasked": extra.get("prior_unmasked"),
        "warnings": warnings,
    }
    same_text = extra.get("same_text_as")
    if same_text:
        data["same_text_as"] = same_text
        data["other_urls"] = s.other_urls or []
    if extra.get("same_text_material"):
        data["same_text_material"] = extra["same_text_material"]
    if extra.get("returned_to"):
        data["returned_to"] = extra["returned_to"]
    if getattr(args, "refresh", False):
        data["refresh"] = (
            "failed"
            if extra.get("refresh_failed")
            else "parallel"
            if extra.get("parallel")
            else "same"
            if extra.get("rechecked")
            else "new"
            if extra.get("refreshes")
            else "first"
        )
        data["rechecked_at"] = str(s.rechecked_at) if s.rechecked_at else None
        data["refreshes"] = extra.get("refreshes")
        data["claims_on_previous"] = extra.get("claims_on_previous", [])
    if extra.get("refresh_failed"):
        previous = extra["previous"]
        data["previous"] = previous
        message = (
            f"перепроверить {previous} не удалось: {extra['refresh_failed']}. "
            f"Запись {previous} и её снимок не изменены, цитаты по ним действуют."
        )
        err = CliError(message, warnings)
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, **data, "error": err.message}, ensure_ascii=False))
            return 1
        return fail(args, err)
    marks = []
    if s.suspicious:
        marks.append("подозрительный")
    if closed:
        marks.append("признаки закрытых данных: " + ", ".join(closed))
    if s.access == "получен":
        if extra.get("returned_to"):
            head = (
                f"Источник {s.id}: страница вернулась к тексту этого снимка (запрос шёл по адресу источника "
                f"{extra['previous']}); новый источник не создан, отметка перепроверки поставлена у {s.id} "
                f"({s.rechecked_at})."
            )
        elif extra.get("rechecked"):
            head = f"Источник {s.id}: перепроверен, текст тот же (проверено {s.rechecked_at})."
        elif extra.get("refreshes"):
            head = (
                f"Источник {s.id}: текст изменился, сохранён новый снимок; он перепроверяет "
                f"{extra['refreshes']}, прежний снимок и его цитаты не тронуты."
            )
        else:
            head = (
                f"Источник {s.id}: "
                + ("снимок уже сохранён" if cached or same_text else "снимок сохранён")
                + "."
            )
        lines = [
            head,
            f"Снимок: {data['snapshot_path']}",
            f"Адрес: {data['final_url'] or s.url}",
            f"Регион: {data['region'] or 'не указан'}",
            f"Доступ: {s.access}",
            "Извлечение: "
            + str(data["extractor"])
            + (" (запасной путь: весь видимый текст)" if data["extraction_fallback"] else ""),
            "Пометки: " + ("; ".join(marks) if marks else "нет"),
        ]
        if cached and not getattr(args, "refresh", False):
            lines.append(
                "Страница не загружалась заново: адрес уже записан. Чтобы перечитать её (страница изменилась "
                "или снимок снят в прежней версии программы), `strateg fetch --refresh <адрес>`."
            )
        if same_text:
            lines.append(
                f"Новый источник не создан: текст совпадает с {same_text} (другой адрес); "
                f"адрес добавлен к {same_text}."
            )
        if extra.get("refreshes") or extra.get("returned_to"):
            on_old = extra["claims_on_previous"]
            lines.append(
                f"Утверждения, которые стоят на {extra.get('refreshes') or extra['previous']}: "
                + (", ".join(on_old) if on_old else "нет")
                + (
                    ". Решает агент: сверить со снимком страницы, заменить (`claim replace`), "
                    "снять (`claim revoke`) или оставить."
                    if on_old
                    else ""
                )
            )
        lines += [f"Внимание: {w}" for w in warnings]
        return emit(args, data, lines)
    data["note"] = s.access_note
    err = CliError(f"{s.id}: {s.access}. {s.access_note}", warnings)
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, **data, "error": err.message}, ensure_ascii=False))
        return 1
    return fail(args, err)


def cmd_source_confirm(args: argparse.Namespace) -> int:
    """Человек подтвердил, что материал с признаками закрытых данных общедоступный."""
    p = _open(args)
    sid = args.id.strip()
    with proj.transaction(p):
        source = next((s for s in p.sources.sources if s.id == sid), None)
        if source is None:
            raise CliError(f"источника {sid} нет в реестре")
        if not source.closed_markers:
            return emit(
                args, {"source": sid, "changed": False}, [f"У источника {sid} нет признаков закрытых данных."]
            )
        if source.closed_confirmed:
            return emit(args, {"source": sid, "changed": False}, [f"Источник {sid} уже подтверждён."])
        source.closed_confirmed = True
        p.save_sources()
        p.add_event(
            p.event(
                kind="действие",
                name="strateg source confirm",
                idempotency_hash=proj.key_hash("source confirm", sid),
                status="выполнено",
                duration_s=0,
                message=f"{sid}: человек подтвердил, что материал общедоступный "
                f"(признаки: {', '.join(source.closed_markers)})",
            )
        )
        p.save_state()
    return emit(
        args,
        {"source": sid, "changed": True},
        [f"Источник {sid} подтверждён как общедоступный, из него можно цитировать."],
    )


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


class DisputeIn(_In):
    claim: ClaimId
    reason: Text


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
    dispute: DisputeIn | None = None
    calc: CalcRef | None = None


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
        raise CliError("входной файл не в кодировке UTF-8") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (строка {mark.line + 1})" if mark else ""
        raise CliError(f"входной файл не читается как YAML или JSON{where}") from exc


def claim_key(
    c_text: str,
    kind: str,
    quotes: list[tuple[str, str]],
    subject: str,
    period: str | None,
    region: str | None,
    basis: list[str],
) -> str:
    """Ключ идемпотентности `claim add`: хеш текста, типа, предмета, периода, региона, оснований и цитат."""
    return proj.key_hash(
        "claim",
        norm(c_text),
        kind,
        sorted((s, quotelib.norm_text(q)) for s, q in quotes),
        norm(subject),
        norm(period or ""),
        norm(region or ""),
        sorted(basis),
    )


def _key_of_input(c: ClaimIn) -> str:
    return claim_key(
        c.text, c.type, [(q.source, q.text) for q in c.quotes], c.subject, c.period, c.region, c.basis
    )


def _key_of_claim(c: Claim) -> str:
    return claim_key(
        c.text, c.type, [(q.source, q.text) for q in c.quotes], c.subject, c.period, c.region, c.basis
    )


def _locator_key(locator: str) -> str:
    """Место цитаты для сравнения: «стр. 3», «с. 3, абзац 2» и «3» — одна страница."""
    page = _locator_page(locator)
    return str(page) if page is not None else norm(locator)


def _extras(c: ClaimIn | Claim) -> dict[str, Any]:
    """Входные поля, которые не входят в ключ `claim add`: по ним повтор отличают от дубля."""
    out: dict[str, Any] = {
        "origin": _default_origin(c),
        "forecast_range": c.forecast_range.model_dump() if c.forecast_range else None,
        "dispute": (c.dispute.claim, norm(c.dispute.reason)) if c.dispute else None,
        "check_method": norm(c.check_method or ""),
        "taken_from": norm(c.taken_from or ""),
        "plan_question": norm(c.plan_question or ""),
        "calc": (c.calc.name, c.calc.key) if c.calc else None,
    }
    for i, q in enumerate(sorted(c.quotes, key=lambda q: (q.source, quotelib.norm_text(q.text))), start=1):
        out[f"publisher (цитата {i})"] = norm(q.publisher or "")
        out[f"publisher_kind (цитата {i})"] = q.publisher_kind
        out[f"self_reported (цитата {i})"] = q.self_reported
        out[f"locator (цитата {i})"] = _locator_key(q.locator) if q.locator else None
    return out


def _differs(recorded: dict[str, Any], incoming: dict[str, Any]) -> str | None:
    """Первое входное поле, которым повтор отличается от записанного. Пустое место цитаты не спорит:
    у PDF оно определяется по снимку."""
    for name, value in incoming.items():
        if name.startswith("locator") and value is None:
            continue
        if recorded.get(name) != value:
            return name
    return None


def _default_origin(c: ClaimIn) -> str:
    if c.origin:
        return c.origin
    if c.type == "синтетика":
        return "синтетика"
    return "источник" if c.type == "факт" or c.quotes else "агент"


MIN_FACT_QUOTE_WORDS = 3
MIN_FACT_QUOTE_CHARS = 20


def _locator_page(locator: str) -> int | None:
    m = re.search(r"\d+", locator)
    return int(m.group()) if m else None


def _pdf_quote(
    text: str, q: QuoteIn, where: str, errors: list[str]
) -> tuple[quotelib.QuoteMatch, str] | None:
    """Цитата PDF: страница всегда определяется по снимку; указанное место должно с ней совпасть."""
    hits = quotelib.pages_with_quote(q.text, text)
    if not hits:
        spans = quotelib.check_quote(q.text, text, soft_wrap=True)
        reason = (
            "цитата идёт через границу страниц: цитируйте текст одной страницы"
            if spans
            else quotelib.explain_miss(q.text, text, soft_wrap=True)
        )
        errors.append(f"{where}: цитата не найдена целиком ни на одной странице PDF: {reason}")
        return None
    if q.locator is None:
        number, found = max(hits, key=lambda h: h[1].similarity)
    else:
        wanted = _locator_page(q.locator)
        if wanted is None:
            errors.append(f"{where}: место «{q.locator}» не разобрать, пишите «стр. N»")
            return None
        chosen = [h for h in hits if h[0] == wanted]
        if not chosen:
            pages = ", ".join(str(n) for n, _ in hits)
            errors.append(
                f"{where}: в locator указана «{q.locator}», а цитата найдена на странице: {pages}; "
                "место определяется по снимку"
            )
            return None
        number, found = chosen[0]
    return found, f"стр. {number}"


def _line_locator(src: SourceModel, q: QuoteIn, text: str, where: str, errors: list[str]) -> str | None:
    """Место цитаты из расшифровки: строка снимка определяется по тексту, указанная должна с ней совпасть.

    Цитата одной строки: через границу строк она не проходит. В синтетической сессии цитата — из ответа
    респондента, а не из вопроса или персоны.
    """
    hits = quotelib.lines_with_quote(q.text, text)
    if src.kind == "синтетика":
        answers = [h for h in hits if quotelib.ANSWER_LINE.match(h[1])]
        if hits and not answers:
            errors.append(
                f"{where}: цитата стоит в вопросе или в описании персоны, а не в ответе респондента: "
                "цитируйте строку «Ответ n: …»"
            )
            return None
        hits = answers
    if not hits:
        errors.append(f"{where}: цитата не умещается в одну строку расшифровки: цитируйте текст одной строки")
        return None
    numbers = [n for n, _ in hits]
    if q.locator is None:
        if len(numbers) > 1:
            errors.append(
                f"{where}: цитата есть в строках {', '.join(map(str, numbers))}: укажите locator «строка N»"
            )
            return None
        return f"строка {numbers[0]}"
    wanted = _locator_page(q.locator)
    if wanted is None:
        errors.append(f"{where}: место «{q.locator}» не разобрать, пишите «строка N»")
        return None
    if wanted not in numbers:
        errors.append(
            f"{where}: в locator указана «{q.locator}», а цитата найдена в строке: "
            f"{', '.join(map(str, numbers))}; место определяется по снимку"
        )
        return None
    return f"строка {wanted}"


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
                f"{where}: в источнике {src.id} найдены признаки закрытых данных, человек не подтвердил, "
                f"что материал общедоступный (`strateg source confirm {src.id}`)"
            )
            continue
        if src.anonymized and not src.anonymization_confirmed and src.path is not None:
            errors.append(
                f"{where}: в материале {src.id} найдены {safety.found_what(src.anonymized)}, "
                "человек не подтвердил замены "
                f"(`strateg add --confirm {src.id}`)"
            )
            continue
        if src.text_path is None or src.text_sha256 is None:
            errors.append(f"{where}: у источника {src.id} нет снимка текста (доступ: «{src.access}»)")
            continue
        if c.type == "факт" and src.kind == "синтетика":
            errors.append(f"{where}: источник {src.id} — синтетика, фактом она не становится")
            continue
        if src.kind == "синтетика" and c.type != "синтетика":
            errors.append(
                f"{where}: источник {src.id} — синтетика: утверждение на нём только типа «синтетика», "
                f"а не «{c.type}»; для гипотезы укажите утверждение-синтетику в basis"
            )
            continue
        if c.type == "синтетика" and src.kind != "синтетика":
            errors.append(
                f"{where}: утверждение типа «синтетика» цитирует только источник вида «синтетика» "
                f"(`strateg synth add`), а {src.id} — «{src.kind}»"
            )
            continue
        if quotelib.PAGE_MARK_INLINE.search(q.text):
            errors.append(f"{where}: в цитате метка страницы «[стр. N]»: цитируйте текст без меток")
            continue
        if c.type == "факт" and (
            quotelib.word_count(q.text) < MIN_FACT_QUOTE_WORDS
            or len(" ".join(q.text.split())) < MIN_FACT_QUOTE_CHARS
        ):
            errors.append(
                f"{where}: цитата слишком короткая: у факта она не короче {MIN_FACT_QUOTE_WORDS} слов "
                f"и {MIN_FACT_QUOTE_CHARS} знаков, иначе одно слово «подтвердит» любое утверждение"
            )
            continue
        path = p.root / src.text_path
        if not path.is_file() or proj.sha256_bytes(path.read_bytes()) != src.text_sha256:
            errors.append(f"{where}: снимок {src.text_path} изменён или не найден; загрузите источник заново")
            continue
        text = path.read_bytes().decode("utf-8")
        locator = q.locator
        if src.kind == "PDF" and quotelib.split_pages(text):
            checked = _pdf_quote(text, q, where, errors)
            if checked is None:
                continue
            found, locator = checked
        else:
            found = quotelib.check_quote(q.text, text)
            if found is None:
                errors.append(
                    f"{where}: цитата не найдена в снимке {src.id}: {quotelib.explain_miss(q.text, text)}"
                )
                continue
            if src.kind in quotelib.LINE_KINDS and quotelib.numbered_lines(text):
                locator = _line_locator(src, q, text, where, errors)
                if locator is None:
                    continue
            elif src.kind in LOCATOR_KINDS and locator is None:
                errors.append(f"{where}: для источника вида «{src.kind}» нужно место цитаты (locator)")
                continue
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
    if c.type == "факт" and len(out) == len(c.quotes):
        in_claim = set(quotelib.numbers(quotelib.norm_text(c.text)))
        in_quotes: set[str] = set()
        for q in c.quotes:
            in_quotes |= set(quotelib.numbers(quotelib.norm_text(q.text)))
        missing = sorted(in_claim - in_quotes)
        if c.calc is not None:  # число из расчёта в цитате не стоит: его сверяет ссылка на расчёт
            found, value = calclib.calc_value(p.root, c.calc)
            if found and calclib.is_number(value):
                from_calc = {m.lstrip("+-") for m in calclib.matching_numbers(c.text, value)}
                missing = [m for m in missing if m.lstrip("+-") not in from_calc]
        if missing:
            errors.append(
                f"{label}: числа из текста утверждения нет ни в одной цитате: {', '.join(missing)}; "
                "приведите цитату с этими числами или уберите их из утверждения"
            )
            return []
    return out


def _make_claim(
    p: Project,
    c: ClaimIn,
    label: str,
    cid: str,
    known_ids: set[str],
    errors: list[str],
    *,
    attempt: int,
    by: str = "агент",
    replaces: str | None = None,
) -> Claim | None:
    """Новое утверждение из входа с теми же проверками для `claim add` и `claim replace`.

    Ошибки дописываются в `errors`; при ошибке возвращается None.
    """
    before = len(errors)
    for ref in c.basis:
        if ref not in known_ids:
            errors.append(f"{label}, поле basis: утверждения {ref} нет в реестре")
    if c.dispute is not None and c.dispute.claim not in known_ids:
        errors.append(f"{label}, поле dispute: утверждения {c.dispute.claim} нет в реестре")
    if c.type == "синтетика" and not c.quotes:
        errors.append(
            f"{label}: утверждение типа «синтетика» записывается с цитатой ответа респондента: "
            "сессия — `strateg synth add`, цитата со строкой «строка N»"
        )
    quotes = _build_quotes(p, c, label, errors)
    if len(errors) > before:
        return None
    suspicious_sources = {s.id for s in p.sources.sources if s.suspicious}
    try:
        return Claim(
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
            status_history=[StatusChange(status="действует", at=proj.now(), by=by)],  # type: ignore[arg-type]
            replaces=replaces,
            plan_question=c.plan_question,
            dispute=Dispute(claim=c.dispute.claim, reason=c.dispute.reason) if c.dispute else None,
            step=c.step or p.state.step,
            attempt=attempt,
            calc=c.calc.model_copy() if c.calc else None,
        )
    except ValidationError as exc:
        errors.extend(_format(label, exc))
        return None


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
    results: list[dict[str, Any]] = []
    # Номера У-n, повторы и цитаты строятся по реестру, перечитанному под блокировкой: параллельная команда
    # не получит тот же номер и не затрёт чужое утверждение.
    with proj.transaction(p):
        recorded = {_key_of_claim(c): (c.id, _extras(c)) for c in p.claims.claims}
        new_claims: list[tuple[Claim, str]] = []
        batch_keys: dict[str, str] = {}
        next_no = max((int(c.id.split("-")[1]) for c in p.claims.claims), default=0) + 1
        attempt = max(_attempts(p, p.state.step), 1)
        for c, label in prepared:
            key = _key_of_input(c)
            if key in recorded:
                cid, extras = recorded[key]
                field = _differs(extras, _extras(c))
                if field:
                    errors.append(
                        f"{label}: такое утверждение уже записано как {cid}, но поле {field} отличается; "
                        "записанное в этой версии не исправляется: запишите новое утверждение "
                        "с другим текстом, предметом, периодом или цитатой"
                    )
                else:
                    results.append({"id": cid, "duplicate": True, "type": c.type})
                continue
            cid = f"У-{next_no}"
            known_ids = {x.id for x in p.claims.claims} | set(batch_keys.values())
            claim = _make_claim(p, c, label, cid, known_ids, errors, attempt=attempt)
            if claim is None:
                continue
            next_no += 1
            batch_keys[key] = cid
            recorded[key] = (cid, _extras(c))
            new_claims.append((claim, key))
            results.append({"id": cid, "duplicate": False, "type": c.type, "suspicious": claim.suspicious})
        if errors:
            raise CliError("утверждения не записаны", errors)
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
        if new_claims:
            taint = synthetic_claims(p.claims.claims, p.sources)
            by_result = {r["id"]: r for r in results}
            for claim, _ in new_claims:
                found = _synthetic_warnings(claim, taint)
                if found:
                    by_result[claim.id]["warnings"] = found
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
        lines += [f"  внимание: {r['id']}: {w}" for w in r.get("warnings", [])]
    return emit(args, {"claims": results}, lines)


_PERSONA_WORD = re.compile(r"респондент|персон", re.IGNORECASE)


def _synthetic_warnings(c: Claim, taint: dict[str, str]) -> list[str]:
    """Предупреждения о синтетике: общий текст читается как вывод о людях, гипотеза на ней не проверена."""
    out: list[str] = []
    if c.type == "синтетика" and not _PERSONA_WORD.search(c.text):
        out.append(
            "текст синтетики читается как вывод о людях: назовите модельного респондента или персону "
            "(«Модельный респондент … сказал …»), в поиске и пересказе тип теряется"
        )
    if c.type == "гипотеза" and taint.get(c.id) == "derived":
        out.append(
            "гипотеза опирается только на ответы модельных респондентов: не проверена на людях, "
            "основанием рекомендации и решения она не станет"
        )
    return out


# «VP-2»: латинская V вместо русской В (общий разбор «почти номеров» её не знает)
_LATIN_PLAN_NUMBER = re.compile(r"[VВB][PП]\s*[-–—]\s*[0-9]+", re.IGNORECASE)


def cmd_claim_list(args: argparse.Namespace) -> int:
    p = _open(args)
    step = _norm_step(args.step) if args.step else None
    question = None
    if args.plan_question:
        value = args.plan_question.strip()
        if (_PLAN_LIKE.fullmatch(value) or _LATIN_PLAN_NUMBER.fullmatch(value)) and not _PLAN_REF.fullmatch(
            value
        ):
            raise UsageError(
                f"номер вопроса плана «{value}» не подходит: нужен вид ВП-1 (буквы русские заглавные, "
                f"без нуля впереди){latin_hint(value)}"
            )
        question = norm(value)
    claims = [
        c
        for c in p.claims.claims
        if (not args.type or c.type == args.type)
        and (step is None or c.step == step)
        and (question is None or norm(c.plan_question or "") == question)
        and (not args.status or c.status == args.status)
    ]
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
            "attempt": c.attempt,
            "plan_question": c.plan_question,
            "basis": c.basis,
            "replaces": c.replaces,
            "replaced_by": c.replaced_by,
            "dispute": c.dispute.model_dump() if c.dispute else None,
        }
        for c in claims
    ]
    lines = [
        f"{d['id']} [{d['type']}, {d['status']}] {d['text']}"
        + (f" (источники: {', '.join(d['sources'])})" if d["sources"] else "")
        for d in data
    ] or ["Утверждений нет."]
    return emit(args, {"claims": data}, lines)


def _claim_ref(text: str) -> str:
    """Номер утверждения из аргумента: `У-3`; латинская буква вместо русской — ошибка с подсказкой."""
    value = text.strip()
    if not re.fullmatch(r"У-[1-9][0-9]*", value):
        raise UsageError(
            f"номер утверждения «{value}» не подходит: нужен вид У-1 (буква русская){latin_hint(value)}"
        )
    return value


def _find_claim(p: Project, cid: str) -> Claim:
    claim = next((c for c in p.claims.claims if c.id == cid), None)
    if claim is None:
        raise CliError(f"утверждения {cid} нет в реестре: список — `strateg claim list`")
    return claim


def _load_output(p: Project, kind: str) -> Any:
    """Разобранный файл выхода или None, если его нет или он не разбирается."""
    spec = outputs.KINDS[kind]
    path = p.root / spec.file
    if not path.is_file():
        return None
    try:
        return spec.model.model_validate(proj.read_yaml(path), by_name=False)
    except (CliError, ValidationError):
        return None


def claim_usages(p: Project, cid: str) -> list[str]:
    """Где в файлах выхода и реестре стоит ссылка на утверждение: варианты, обещания, строки плана."""
    out: list[str] = []
    opts = _load_output(p, "options")
    if opts is not None:
        for o in opts.options:
            if any(obj.basis == cid for obj in o.objections):
                out.append(f"вариант {o.id}: основание возражения")
            if cid in (o.comparison.effect.forecast, o.comparison.first_signal.forecast):
                out.append(f"вариант {o.id}: прогноз в сравнении")
        if cid in opts.recommendation.claims:
            out.append(f"рекомендация (вариант {opts.recommendation.option}): основание")
    prom = _load_output(p, "promises")
    if prom is not None:
        out += [f"обещание {x.id}: опора на факт" for x in prom.promises if cid in x.claims]
    ver = _load_output(p, "verification")
    if ver is not None:
        out += [f"строка плана проверки {r.id}: гипотеза" for r in ver.rows if r.hypothesis == cid]
        out += [
            f"пре-мортем, строка плана проверки {m.row}: причина" for m in ver.premortem if m.claim == cid
        ]
    gaps = _load_output(p, "gaps")
    if gaps is not None:
        for g in gaps.gaps:
            if cid in (g.assumption, g.hypothesis) or cid in g.closed_by:
                out.append(f"пробел {g.id}")
        out += [f"перечень показателей: «{m.kind}»" for m in gaps.metric_coverage if cid in (m.claims or [])]
    dec = _load_output(p, "decisions")
    if dec is not None:
        out += [f"решение {d.id}" for d in dec.decisions if cid in d.basis or cid in d.assumptions]
    for c in p.claims.claims:
        if cid in c.basis or (c.dispute is not None and c.dispute.claim == cid):
            out.append(f"утверждение {c.id}: основание или спор")
    return out


def _usage_lines(places: list[str]) -> list[str]:
    if not places:
        return []
    return [
        "Утверждение используется: " + "; ".join(places) + ".",
        "Перепишите эти места на действующее утверждение и запишите файлы командой `strateg write`: "
        "рекомендация и обещание с отозванным или заменённым утверждением не проходят `strateg check`.",
    ]


def _stale_only(error: str) -> bool:
    """Ошибка «отозванное или заменённое утверждение у рекомендации или обещания»: её чинит `write`."""
    return STALE_REF_NOTE in error


def _by(args: argparse.Namespace) -> str:
    return getattr(args, "by", None) or "агент"


def _correction_event(p: Project, by: str, message: str) -> None:
    """Правка человека (С-4, С-5) — событие «исправление»."""
    if by == "пользователь":
        p.add_event(p.event(kind="исправление", actor="пользователь", message=f"правка человека: {message}"))


def cmd_claim_replace(args: argparse.Namespace) -> int:
    p = _open(args)
    ref = _claim_ref(args.id)
    by = _by(args)
    raw = _read_input(args.file)
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        raise CliError("для замены нужно одно утверждение: набор полей «ключ: значение»")
    label = "новое утверждение"
    try:
        c = ClaimIn.model_validate(raw)
    except ValidationError as exc:
        raise CliError("замена не записана: файл не по схеме", _format(label, exc)) from exc

    key = _key_of_input(c)
    started = time.monotonic()
    with proj.transaction(p, tolerate=_stale_only):
        old = _find_claim(p, ref)
        if old.status == "заменено":
            new = next((x for x in p.claims.claims if x.id == old.replaced_by), None)
            if new is not None and _key_of_claim(new) == key:
                field = _differs(_extras(new), _extras(c))
                if field:
                    raise CliError(
                        f"{old.id} уже заменено на {new.id}, но поле {field} отличается; записанное не "
                        "исправляется: запишите новое утверждение с другим текстом или цитатой"
                    )
                return emit(
                    args,
                    {"id": new.id, "replaced": old.id, "already_replaced": True},
                    [f"{old.id} уже заменено на {new.id}, повтор ничего не меняет."],
                )
            raise CliError(
                f"{old.id} уже заменено на {old.replaced_by}: заменить можно только действующее утверждение"
            )
        if old.status != "действует":
            raise CliError(
                f"{old.id} имеет статус «{old.status}»: заменить можно только действующее утверждение"
            )
        twin = next((x for x in p.claims.claims if _key_of_claim(x) == key), None)
        if twin is not None and _differs(_extras(twin), _extras(c)) is None:
            # полностью то же утверждение: нового номера не будет
            if twin.id == old.id:
                return emit(
                    args,
                    {"id": old.id, "replaced": old.id, "already_replaced": False, "unchanged": True},
                    [f"{old.id}: такое утверждение уже записано, замена не нужна."],
                )
            raise CliError(
                f"такое утверждение уже записано как {twin.id}: замена не создаёт дубль; "
                "перепишите текст, предмет, период, цитату или другое поле"
            )

        errors: list[str] = []
        cid = f"У-{max(int(x.id.split('-')[1]) for x in p.claims.claims) + 1}"
        claim = _make_claim(
            p,
            c,
            label,
            cid,
            {x.id for x in p.claims.claims},
            errors,
            attempt=max(_attempts(p, p.state.step), 1),
            by=by,
            replaces=old.id,
        )
        if claim is None:
            raise CliError("замена не записана", errors)

        at = proj.now()
        old.status = "заменено"
        old.replaced_by = cid
        old.status_history.append(
            StatusChange(status="заменено", at=at, by=by, reason=f"заменено на {cid}")  # type: ignore[arg-type]
        )
        p.claims.claims.append(claim)
        message = f"{old.id} заменено на {cid}"
        p.add_event(
            p.event(
                step=claim.step,
                kind="действие",
                name="strateg claim replace",
                idempotency_hash=proj.key_hash("claim replace", old.id, key),
                status="выполнено",
                duration_s=round(time.monotonic() - started, 2),
                message=message,
            )
        )
        _correction_event(p, by, message)
        p.save_claims()
        p.save_state()
        places = claim_usages(p, old.id)
    return emit(
        args,
        {"id": cid, "replaced": old.id, "already_replaced": False, "used_in": places, "by": by},
        [f"{cid}: записано ({c.type}), заменяет {old.id}.", *_usage_lines(places)],
    )


def cmd_claim_revoke(args: argparse.Namespace) -> int:
    p = _open(args)
    ref = _claim_ref(args.id)
    by = _by(args)
    reason = args.reason.strip()
    if not reason:
        raise UsageError('причина отзыва пустая: укажите --reason "<текст>"')
    with proj.transaction(p, tolerate=_stale_only):
        claim = _find_claim(p, ref)
        if claim.status == "отозвано":
            return emit(
                args,
                {"id": claim.id, "already_revoked": True},
                [f"{claim.id} уже отозвано, повтор ничего не меняет."],
            )
        if claim.status == "заменено":
            raise CliError(
                f"{claim.id} уже заменено на {claim.replaced_by}: "
                "отозвать можно только действующее утверждение"
            )
        claim.status = "отозвано"
        claim.status_history.append(
            StatusChange(status="отозвано", at=proj.now(), by=by, reason=reason)  # type: ignore[arg-type]
        )
        message = f"{claim.id} отозвано: {reason}"
        p.add_event(
            p.event(
                kind="действие",
                name="strateg claim revoke",
                idempotency_hash=proj.key_hash("claim revoke", claim.id),
                status="выполнено",
                duration_s=0,
                message=message,
            )
        )
        _correction_event(p, by, message)
        p.save_claims()
        p.save_state()
        places = claim_usages(p, claim.id)
    return emit(
        args,
        {"id": claim.id, "already_revoked": False, "used_in": places, "by": by},
        [f"{claim.id} отозвано.", *_usage_lines(places)],
    )


# ---------------------------------------------------------------------------
# write: файлы выхода
# ---------------------------------------------------------------------------


def _keep_released_decisions(p: Project, models: dict[str, Any]) -> None:
    """После выпуска решение из снимка не убирают из `decisions.yaml`: его заменяют или отменяют."""
    model = models.get("decisions")
    released = releaselib.released_decisions(p.root) if model is not None else None
    if released is None:
        return
    number, ids = released
    gone = [i for i in ids if i not in {d.id for d in model.decisions}]
    if gone:
        raise CliError(
            f"файлы выхода не записаны: {', '.join(gone)} было в снимке выпуска {number} "
            "и пропало из decisions.yaml. "
            "Решение не убирают: новое решение пишут с `replaces: <прежнее>`, а у прежнего ставят "
            "`status: заменено` и `replaced_by`; ненужное решение отменяют статусом «отменено»"
        )


def cmd_write(args: argparse.Namespace) -> int:
    p = _open(args)
    by = _by(args)
    contents = outputs.parse_input(_read_input(args.file))
    models: dict[str, Any] = {}
    errors: list[str] = []
    for name, content in contents.items():
        model, errs = outputs.validate(outputs.KINDS[name], content)
        errors += errs
        if model is not None:
            models[name] = model
    if errors:
        raise CliError("файлы выхода не записаны: вход не по схеме", errors)

    started = time.monotonic()
    # Что менять, номера версий и `history/` определяются по файлам, перечитанным под блокировкой.
    # Ошибки в самих записываемых файлах мешают записи, даже если были до неё.
    changes: list[outputs.Change] = []
    same: list[outputs.Kind] = []
    report: list[dict[str, Any]] = []
    warnings: list[str] = []
    mine: re.Pattern[str] | None = None
    try:
        with proj.transaction(p, block=lambda e: mine is not None and mine.match(e) is not None):
            _keep_released_decisions(p, models)
            changes, same = outputs.plan_changes(p, models)
            report = [
                {
                    "kind": ch.kind.name,
                    "file": ch.kind.file,
                    "changed": True,
                    "version": ch.version,
                    "history": ch.history,
                }
                for ch in changes
            ] + [
                {"kind": k.name, "file": k.file, "changed": False, "version": None, "history": None}
                for k in same
            ]
            if not changes:
                return emit(
                    args,
                    {"changed": False, "files": report, "warnings": []},
                    [f"{k.file}: без изменений" for k in same] + ["Ничего не записано: содержимое то же."],
                )
            written = [ch.kind.file for ch in changes] + [k.file for k in same]
            mine = re.compile("^(?:" + "|".join(re.escape(f) for f in written) + ")[,:]")
            for ch in changes:
                outputs.apply_change(p, ch, started, by)
            p.save_state()
    except CliError as err:
        written = [ch.kind.file for ch in changes] + [k.file for k in same]
        raise CliError(err.message, outputs.add_hints(err.details, written)) from err

    with proj.locked(p.root):  # соседняя команда не пишет, пока проект проверяется
        old_errors = check_project(p.root)  # новых ошибок нет: иначе запись откатилась бы
    if old_errors:
        warnings.append(
            f"в проекте остались прежние ошибки проверки: {len(old_errors)}; запись их не добавила "
            "(список — `strateg check`)"
        )
    lines = [f"{ch.kind.file}: {ch.note}" for ch in changes] + [f"{k.file}: без изменений" for k in same]
    lines += [f"Внимание: {w}" for w in warnings]
    return emit(args, {"changed": True, "files": report, "warnings": warnings, "by": by}, lines)


# ---------------------------------------------------------------------------
# step fail, event error
# ---------------------------------------------------------------------------


def cmd_step_fail(args: argparse.Namespace) -> int:
    p = _open(args)
    step = _norm_step(args.step)
    reason = args.reason.strip()
    if not reason:
        raise UsageError('причина пустая: укажите --reason "<текст>"')
    name = proj.step_action_name(step)
    with proj.transaction(p):
        mine = [e for e in p.journal if e.kind == "действие" and e.name == name and e.idempotency_hash]
        if not mine:
            raise CliError(f"шаг {step} не начат: выполните `strateg step start {step}`")
        last = mine[-1]
        if last.status == "ошибка":
            return emit(
                args,
                {"step": step, "already_failed": True},
                [f"Шаг {step} уже отмечен ошибкой, повтор ничего не меняет."],
            )
        if last.status != "начато":
            raise CliError(
                f"шаг {step} не идёт (последняя отметка: «{last.status}»): ошибку отмечают у начатого шага"
            )
        p.add_event(
            p.event(
                step=step,
                kind="действие",
                name=name,
                idempotency_hash=last.idempotency_hash,
                status="ошибка",
                message=reason,
            )
        )
        p.state.next_action = (
            f"Шаг {step} закончился ошибкой: {reason}. "
            f"Повторить `strateg step start {step}` или остановить работу"
        )
        p.save_state()
    return emit(args, {"step": step, "reason": reason}, [f"Шаг {step} отмечен ошибкой: {reason}"])


def cmd_event_error(args: argparse.Namespace) -> int:
    p = _open(args)
    message = args.message.strip()
    if not message:
        raise UsageError('текст ошибки пустой: укажите --message "<текст>"')
    tool = args.tool.strip() if args.tool else None
    with proj.transaction(p):
        step = _norm_step(args.step) if args.step else p.state.step
        p.add_event(
            p.event(
                step=step,
                actor="хук" if tool else "агент",
                kind="ошибка",
                name=tool or None,
                message=message,
            )
        )
        p.save_state()
    where = f"шаг {step}" + (f", {tool}" if tool else "")
    return emit(
        args, {"step": step, "tool": tool, "message": message}, [f"Ошибка записана ({where}): {message}"]
    )


# ---------------------------------------------------------------------------
# add: материалы человека
# ---------------------------------------------------------------------------


def _source_ref(text: str) -> str:
    value = text.strip()
    if not re.fullmatch(r"И-[1-9][0-9]*", value):
        raise UsageError(
            f"номер источника «{value}» не подходит: нужен вид И-1 (буква русская){latin_hint(value)}"
        )
    return value


def _add_data(p: Project, o: materials.Outcome) -> dict[str, Any]:
    s, rep = o.source, o.report
    return {
        "source": s.id,
        "kind": s.kind,
        "duplicate": o.duplicate,
        "path": s.path,
        "text_path": s.text_path,
        "snapshot_path": _snapshot_path(p, s),
        "sha256": s.text_sha256,
        "material_sha256": s.material_sha256,
        "consent": s.consent,
        "anonymized": list(s.anonymized),
        "replacements": [{"label": r.label, "line": r.line} for r in (rep.replacements if rep else [])],
        "keys": dict(rep.key_kinds) if rep else {},
        "names": [{"name": n, "line": line} for n, line in (rep.names if rep else [])],
        "closed_markers": list(s.closed_markers),
        "suspicious": s.suspicious,
        "needs_confirmation": o.needs_confirm,
        "moved": o.moved,
        "warnings": o.warnings,
    }


def cmd_add(args: argparse.Namespace) -> int:
    p = _open(args)
    if args.confirm:
        if args.path or args.kind or args.consent:
            raise UsageError("--confirm задаётся отдельно: `strateg add --confirm И-n`")
        res = materials.confirm(p, _source_ref(args.confirm))
        sid = res["source"]
        if not res["changed"]:
            return emit(args, res, [str(res["note"])])
        lines = [
            f"{sid}: подтверждено человеком."
            if res.get("image")
            else f"{sid}: замены подтверждены, в inputs/ остаётся обезличенная копия."
        ]
        if res["moved"]:
            lines.append(f"Исходник перенесён в {res['moved']} — вне проекта и вне архива.")
        if res["note"]:
            lines.append(str(res["note"]))
        return emit(args, res, lines)
    if not args.path:
        raise UsageError("укажите файл: `strateg add <путь>` или `strateg add --confirm И-n`")
    try:
        file = materials.resolve_material(p, args.path)
    except CliError:
        outcome = materials.moved_duplicate(p, Path(args.path).name)  # исходник с ПДн унёс соседний `add`
        if outcome is None:
            raise
    else:
        outcome = materials.register(p, file, args.kind, args.consent)
    s, rep = outcome.source, outcome.report
    data = _add_data(p, outcome)
    if outcome.duplicate:
        lines = [f"{s.id}: этот файл уже зарегистрирован, повтор отдаёт прежний номер."]
        lines += [f"Внимание: {w}" for w in outcome.warnings]
        return emit(args, data, lines)
    lines = [f"{s.id}: материал зарегистрирован ({s.kind})."]
    if s.consent:
        lines.append(f"Согласие собеседника: {s.consent}.")
    if s.text_path:
        lines.append(f"Текст: {data['snapshot_path']}")
    elif s.kind == "изображение":
        lines.append("Текста нет: изображение модель видит напрямую.")
    if rep is not None and rep.replacements:
        lines.append(
            f"Заменено в обезличенной копии: {len(rep.replacements)} ({', '.join(s.anonymized)}). "
            "Исходник убран из inputs/ в .tmp/ проекта и в работу не идёт."
        )
        for r in rep.replacements[:30]:
            lines.append(f"  строка {r.line}: {r.label} — «{materials.line_context(rep.text, r.line)}»")
        if len(rep.replacements) > 30:
            lines.append(f"  … и ещё {len(rep.replacements) - 30}")
    elif s.kind == "интервью":
        lines.append(
            "Имён для замены не нашлось, но расшифровка идёт путём персональных данных: исходник убран "
            "из inputs/ в .tmp/ проекта и в работу не идёт, пока вы не подтвердите копию."
        )
    elif materials.NAME_CATEGORY in s.anonymized:
        lines.append(
            "В имени файла найдены персональные данные: текст записан под нейтральным именем. "
            "Исходник убран из inputs/ в .tmp/ проекта и в работу не идёт."
        )
    lines += [f"Внимание: {w}" for w in outcome.warnings]
    return emit(args, data, lines)


# ---------------------------------------------------------------------------
# budget set, budget check
# ---------------------------------------------------------------------------


def _warn_share() -> float:
    """Доля бюджета для предупреждения; нет файла настроек — 0,8, статус не должен из-за этого падать."""
    try:
        return budgetlib.load_defaults().budget.warn_share
    except CliError:
        return 0.8


def _positive(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if not (math.isfinite(value) and value > 0):
        raise UsageError(f"{name} должно быть конечным числом больше нуля")
    return value


def cmd_budget_set(args: argparse.Namespace) -> int:
    p = _open(args)
    usd = _positive("--usd", args.usd)
    minutes = _positive("--minutes", args.minutes)
    if usd is None and minutes is None:
        raise UsageError("укажите --usd и/или --minutes: новый лимит денег и времени работы агента")
    with proj.transaction(p):
        b = p.state.budget
        changes: list[str] = []
        if usd is not None and usd != b.limit_usd:
            was = "без лимита" if b.limit_usd is None else _usd(b.limit_usd)
            changes.append(f"деньги {was} -> {_usd(usd)}")
        if minutes is not None and minutes != b.limit_minutes:
            changes.append(f"время {b.limit_minutes:g} -> {minutes:g} мин")
        if not changes:
            return emit(
                args,
                {"changed": False, "budget": budgetlib.as_dict(budgetlib.measure(p), _warn_share())},
                ["Бюджет уже такой, ничего не изменено."],
            )
        message = "бюджет изменён: " + "; ".join(changes)
        if usd is not None:
            b.limit_usd = usd
        if minutes is not None:
            b.limit_minutes = minutes
        p.add_event(p.event(kind="бюджет", message=message))
        if (p.state.next_action or "").startswith("Внеплановая остановка"):
            p.state.next_action = (
                "Бюджет изменён человеком; продолжить работу с шага, на котором остановились"
            )
        _refresh_budget(p)
        p.save_state()
    view = budgetlib.as_dict(budgetlib.measure(p), _warn_share())
    return emit(
        args,
        {"changed": True, "message": message, "budget": view},
        [message[0].upper() + message[1:] + ".", "Бюджет: " + view["text"]],
    )


def cmd_budget_check(args: argparse.Namespace) -> int:
    """Код 0 — действие можно, код 1 — бюджет исчерпан (деньги или время); отказ — событие «бюджет»."""
    p = _open(args)
    action = (args.action or "").strip() or None
    release = budgetlib.is_release(action)
    defaults = budgetlib.load_defaults().budget if release else None
    m = budgetlib.measure(p)
    reasons = budgetlib.exhausted_reasons(m, reserve=defaults.release_reserve if defaults else None)
    view = budgetlib.as_dict(m, _warn_share())
    what = f"«{action}»" if action else "действие"
    if not reasons:
        data = {"allowed": True, "action": action, "reserve": release, "budget": view}
        lines = [f"Бюджет позволяет {what}: " + view["text"] + "."]
        if view["warning"]:
            lines.append("Внимание: " + view["warning"] + ".")
        return emit(args, data, lines)

    reason = " и ".join(reasons)
    spent_usd = "неизвестно" if m.spent_usd is None else _usd(m.spent_usd)
    limit_usd = "без лимита" if m.limit_usd is None else _usd(m.limit_usd)
    summary = (
        f"деньги: потрачено {spent_usd} из {limit_usd}; "
        f"время: {_num(m.spent_minutes)} из {m.limit_minutes:g} мин"
    )
    head = f"бюджет исчерпан ({reason}), отказ в действии {what}:"
    message = f"{head} {summary}"
    # Один отказ по одной причине, пока лимит не менялся: числа в тексте растут и не сравниваются,
    # а событие «бюджет изменён» (и любое другое событие вида «бюджет») начинает счёт заново.
    with proj.transaction(p):
        last = next((e for e in reversed(proj.own_events(p.journal)) if e.kind == "бюджет"), None)
        if not (last is not None and (last.message or "").startswith(head)):
            p.add_event(p.event(kind="бюджет", message=message))
            p.save_state()
    options = (
        "Варианты: добавить бюджет (`strateg budget set --usd <число> --minutes <число>`), сузить задачу или "
        "выпустить то, что есть, с пометкой о неполноте (`strateg budget check --action выпуск`)."
    )
    if getattr(args, "json", False):
        data = {
            "ok": False,
            "allowed": False,
            "action": action,
            "reason": reason,
            "budget": view,
            "error": message,
        }
        print(json.dumps(data, ensure_ascii=False))
        return 1
    return fail(
        args, CliError(f"бюджет исчерпан ({reason}): действие {what} не выполнять", [summary, options])
    )


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def cmd_pack(args: argparse.Namespace) -> int:
    root = proj.resolve_project(getattr(args, "project", None))
    packed = packlib.pack(root, Path(args.out) if args.out else None)
    scanner = {
        packlib.GITLEAKS_USED: "gitleaks использован",
        packlib.GITLEAKS_MISSING: "gitleaks не найден: только встроенные шаблоны",
        packlib.GITLEAKS_FAILED: "gitleaks не отработал: только встроенные шаблоны",
    }[packed.gitleaks]
    return emit(
        args,
        {
            "archive": str(packed.archive),
            "files": packed.files,
            "bytes": packed.bytes,
            "gitleaks": packed.gitleaks,
            "unmasked_snapshots": [old for old, _ in packed.stale],
        },
        [
            f"Проект {root.name} упакован: {packed.archive}",
            f"Файлов: {packed.files}, размер: {max(packed.bytes // 1024, 1)} КБ.",
            "В архиве нет .tmp/, кэша fetch и исходников материалов с персональными данными."
            if not packed.stale
            else "В архиве нет .tmp/, кэша fetch и исходников материалов с персональными данными; "
            "но есть прежние снимки до замены авторов отзывов, имена в них не заменены.",
            f"Секретов не найдено: встроенные шаблоны, {scanner}.",
            *[f"Внимание: {safety.stale_snapshot_note(old, new)}" for old, new in packed.stale],
        ],
    )


# ---------------------------------------------------------------------------
# check, cost, status
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    root = proj.resolve_project(getattr(args, "project", None))
    with proj.locked(root):  # соседняя команда не пишет, пока проект читается: полузаписанного не видно
        errors = check_project(root)
    if errors:
        return fail(args, CliError(f"проверка проекта {root.name}: ошибок {len(errors)}", errors))
    return emit(args, {"project": root.name, "errors": []}, [f"Проект {root.name} в порядке: ошибок нет."])


def cmd_cost_add(args: argparse.Namespace) -> int:
    p = _open(args)
    if args.usd is None and args.source:
        raise UsageError("--source задаётся вместе с --usd")
    if args.usd is not None and not (math.isfinite(args.usd) and args.usd >= 0):
        raise UsageError("--usd должно быть конечным числом не меньше нуля")
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
    with proj.transaction(p):
        if not args.total:
            # Без --message-id каждая запись новая, даже одинаковая: время и число записей
            # (часы Windows грубые).
            message_id = args.message_id or f"auto-{time.time_ns() // 1000}-{len(p.costs)}"
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
    from strateg import classify  # импорт здесь: classify импортирует этот модуль

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
    running = None  # сколько минут идёт начатый и не завершённый шаг (без ожидания ответов человека)
    if p.state.step in [e.step for e in _open_steps(p).values()]:
        running = round((proj.work_seconds(p.journal, at=proj.now()) - proj.work_seconds(p.journal)) / 60, 2)
    open_steps = [e.step for e in _open_steps(p).values()]
    waiting = [g for g in GATES if p.state.gates.get(g) == "ждут ответа"]  # type: ignore[call-overload]
    paused = proj.pause_kind(p.journal) if p.state.step in open_steps else None
    if waiting:
        nxt = (
            f"ждём ответа на {waiting[0]}: следующее сообщение человека и есть ответ; "
            f"запишите его командой `strateg gate answer {waiting[0]} --answer '<ответ>'`"
        )
    else:
        nxt = p.state.next_action
    return {
        "project": p.id,
        "mode": p.state.mode,
        "step": p.state.step,
        "step_name": STEP_NAMES[p.state.step],
        "step_in_progress": p.state.step in open_steps,
        "paused": paused,
        "waiting_gates": waiting,
        "gates": dict(p.state.gates),
        "sources": {
            "total": len(p.sources.sources),
            "by_kind": by_kind,
            "unavailable": sum(1 for s in p.sources.sources if s.access != "получен"),
            "suspicious": [s.id for s in p.sources.sources if s.suspicious],
            "unmasked_snapshots": sorted(safety.snapshots_before_masking(p.sources.sources)),
            "closed_unconfirmed": [
                s.id for s in p.sources.sources if s.closed_markers and not s.closed_confirmed
            ],
        },
        "claims": {
            "active": len(active),
            "total": len(p.claims.claims),
            "by_type": by_type,
            "without_quote": without_quote,
            "verdicts": classify.summary(p),
        },
        "failures": sum(
            1
            for e in proj.own_events(p.journal)
            if e.kind == "ошибка" or (e.kind == "действие" and e.status == "ошибка")
        ),
        "work_minutes": round(proj.work_seconds(p.journal, at=proj.now()) / 60, 2),
        "running_minutes": running,
        "clock_minutes": round(proj.clock_seconds(p.journal) / 60, 2),
        "limit_minutes": p.state.budget.limit_minutes,
        "cost_usd": total_usd,
        "cost_precision": precision,
        "cost_complete": complete,
        "limit_usd": p.state.budget.limit_usd,
        "budget": budgetlib.as_dict(budgetlib.measure(p), _warn_share()),
        "pending_confirmation": [
            s.id
            for s in p.sources.sources
            if s.path is not None
            and (
                (s.anonymized and not s.anonymization_confirmed)
                or (s.kind == "изображение" and not s.no_personal_data_confirmed)
            )
        ],
        "next_action": nxt,
    }


PAUSE_TEXT = {
    proj.PAUSE_GATE: "ждём ответа на {gates}",
    proj.PAUSE_STOP: "ждём ответ человека на внеплановую остановку",
}


def _step_state(d: dict[str, Any]) -> str:
    """Пометка у шага: «в работе» или «на паузе: чего ждём» (ворота или остановка)."""
    if not d["step_in_progress"]:
        return ""
    if d["paused"] is None:
        return " · в работе"
    gates = ", ".join(d["waiting_gates"]) or "воротах"
    return " · на паузе: " + PAUSE_TEXT[d["paused"]].format(gates=gates)


def _clock_note(d: dict[str, Any]) -> str:
    """Время работы и время по часам: ожидание ответов человека входит только во второе."""
    return (
        f"время работы {_num(d['work_minutes'])} мин, "
        f"по часам {_num(d['clock_minutes'])} мин (с ожиданием ответов и перерывами)"
    )


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
    if d["budget"]["warning"]:
        parts.append(d["budget"]["warning"])
    return " · ".join(parts)


def _paused_steps(p: Project) -> dict[str, JournalEvent]:
    """Шаги, которые ждут человека: после их «начато» последнее событие хода проекта — открытые ворота без
    ответа или «внеплановая остановка» (события хуков и «прервано» сводки не в счёт).
    Ключ — идемпотентность шага."""
    if proj.pause_kind(p.journal) is None:
        return {}
    return _open_steps(p)


def _resume(p: Project) -> list[JournalEvent]:
    """Возобновление работы: «прервано» для начатых и не завершённых действий.

    Шаг, который ждал ответа человека (ворота или остановка), не прерывается: работа идёт дальше с того же
    «начато». У настоящего перерыва «прервано» получает длительность — работу шага до последнего события
    проекта; простой до возобновления в неё не входит. Событие пишется один раз: после него последняя отметка
    действия уже не «начато». «Изменено вне агента» пишет `save_state` (так же, как у любой команды).
    Возвращает прерванные действия.
    """
    with proj.transaction(p, rollback=False):
        last: dict[str, JournalEvent] = {}
        for e in p.journal:
            if e.kind == "действие" and e.idempotency_hash:
                last[e.idempotency_hash] = e
        paused = _paused_steps(p)
        interrupted = [(h, e) for h, e in last.items() if e.status == "начато" and h not in paused]
        if not interrupted and not p.outside:
            return []
        # длительность считается по журналу до записи «прервано»: иначе концом работы
        # стало бы время возобновления
        seconds = {
            h: proj.interrupted_seconds(p.journal, e) if (e.name or "").startswith("шаг ") else None
            for h, e in interrupted
        }
        for h, e in interrupted:
            p.add_event(
                p.event(
                    step=e.step,
                    kind="действие",
                    name=e.name,
                    idempotency_hash=h,
                    status="прервано",
                    duration_s=seconds[h],
                    message=proj.RESUME_NOTE,
                )
            )
        p.save_state()  # пишет «изменено вне агента» и принимает хеши: повторный `status` ничего не запишет
    return [e for _, e in interrupted]


def _brief(args: argparse.Namespace, p: Project, *, peek: bool) -> int:
    """Сводка «где остановились»: не больше 10 строк. Её вызывает хук начала сеанса и правило AGENTS.md.

    С `peek` ничего не пишется: ни «прервано», ни «изменено вне агента». Так сводку берут посреди работы
    (например, после сжатия контекста): идущий шаг прерванным не становится.
    """
    changed = list(p.outside)
    interrupted = [] if peek else _resume(p)
    d = _status_data(p)
    present = [k.name for k in outputs.KINDS.values() if (p.root / k.file).is_file()]
    frame_note = ""
    frame_version = None
    if "frame" in present:
        try:
            frame_version = FrameFile.model_validate(
                proj.read_yaml(p.root / FRAME_FILE), by_name=False
            ).version
        except (CliError, ValidationError):
            frame_version = None
        draft = not outputs.b1_answered(p.journal)
        frame_note = " (черновик)" if draft else (f" (версия {frame_version})" if frame_version else "")
    nxt = d["next_action"]
    resume = [f"strateg step start {e.step}" for e in interrupted if (e.name or "").startswith("шаг ")]
    if resume and not d["waiting_gates"]:
        nxt = f"начать прерванный шаг заново: `{resume[-1]}`; записанное — в `strateg claim list --step …`"
    lines = [
        f"Проект: {d['project']} · режим: {d['mode']}",
        f"Шаг: {d['step']} ({d['step_name']})" + _step_state(d),
        "Ворота ждут ответа: " + ", ".join(d["waiting_gates"])
        if d["waiting_gates"]
        else "Ворота: ответов не ждут",
        f"Дальше: {nxt or 'не задано'}",
    ]
    if interrupted:
        names = ", ".join(sorted({e.name or "действие" for e in interrupted}))
        lines.append(f"Прервано (записано «прервано»): {names}")
    if changed:
        lines.append("Изменено вне агента: " + ", ".join(rel for rel, _ in changed))
    if peek and changed:
        lines.append("Сводка без записи (--peek): событие «изменено вне агента» запишет следующая команда.")
    lines.append(
        "Бюджет: "
        + d["budget"]["text"]
        + (f" ({d['budget']['warning']})" if d["budget"]["warning"] else "")
        + " · "
        + _clock_note(d)
    )
    lines.append(
        "Файлы выхода: " + (", ".join(f"{k}{frame_note if k == 'frame' else ''}" for k in present) or "нет")
    )
    env_notes = hookflags.notes(os.environ)
    if env_notes:
        lines.append("Внимание: " + "; ".join(env_notes))
    data = {
        "project": d["project"],
        "mode": d["mode"],
        "step": d["step"],
        "step_name": d["step_name"],
        "step_in_progress": d["step_in_progress"],
        "paused": d["paused"],
        "waiting_gates": d["waiting_gates"],
        "next_action": nxt,
        "interrupted": [{"name": e.name, "step": e.step} for e in interrupted],
        "changed_outside": [{"path": rel, "why": why} for rel, why in changed],
        "peek": peek,
        "outputs": present,
        "frame_version": frame_version,
        "frame_draft": "frame" in present and not outputs.b1_answered(p.journal),
        "budget": d["budget"],
        "work_minutes": d["work_minutes"],
        "clock_minutes": d["clock_minutes"],
        "env_notes": env_notes,
    }
    return emit(args, data, lines)


NO_PROJECT_LINE = "Стратег: проекта нет"


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "peek", False) and not getattr(args, "brief", False):
        raise UsageError("--peek задаётся вместе с --brief")
    try:
        p = _open(args)
    except NoActiveProject:
        if not getattr(args, "line", False):
            raise
        # строка статуса стоит в окне всегда, и без проекта она не ошибка
        return emit(args, {"line": NO_PROJECT_LINE, "project": None}, [NO_PROJECT_LINE])
    if getattr(args, "brief", False):
        return _brief(args, p, peek=getattr(args, "peek", False))
    d = _status_data(p)
    d["line"] = status_line(d)
    if args.line:
        return emit(args, {"line": d["line"]}, [d["line"]])
    cost = (
        "нет данных"
        if d["cost_usd"] is None
        else f"{_usd(d['cost_usd'])} ({d['cost_precision']}{'' if d['cost_complete'] else ', неполно'})"
    )
    running = ""
    if d["running_minutes"] is not None:
        running = f" · шаг идёт {round(d['running_minutes'])} мин"
        if d["running_minutes"] > 60:
            running += " (больше часа: возможно, был перерыв, и время завышено)"
    kinds = ", ".join(f"{k} {v}" for k, v in d["sources"]["by_kind"].items())
    types = ", ".join(f"{k} {v}" for k, v in d["claims"]["by_type"].items())
    lines = [
        f"Проект: {d['project']} · режим: {d['mode']}",
        f"Шаг: {d['step']} ({d['step_name']})" + _step_state(d),
        "Ворота: " + ", ".join(f"{g} {s}" for g, s in d["gates"].items()),
        f"Источники: {d['sources']['total']}"
        + (f" ({kinds})" if kinds else "")
        + (f", недоступны: {d['sources']['unavailable']}" if d["sources"]["unavailable"] else ""),
        f"Утверждения: {d['claims']['active']} действуют"
        + (f" ({types})" if types else "")
        + (
            f"; смысл цитат проверен: {d['claims']['verdicts']['checked']}, "
            f"без вердикта: {d['claims']['verdicts']['unchecked']}"
            if d["claims"]["verdicts"]["checked"]
            else ""
        ),
        f"Время работы: {_num(d['work_minutes'])} из {d['limit_minutes']:g} мин{running} · "
        f"по часам {_num(d['clock_minutes'])} мин (с ожиданием ответов и перерывами) · стоимость: {cost}",
        "Бюджет: " + d["budget"]["text"],
    ]
    if d["budget"]["warning"]:
        lines.append("Внимание: " + d["budget"]["warning"])
    if d["pending_confirmation"]:
        lines.append(
            "Внимание: материалы ждут подтверждения человека (`strateg add --confirm`): "
            + ", ".join(d["pending_confirmation"])
        )
    if d["sources"]["suspicious"]:
        lines.append(
            "Внимание: источники с пометкой «подозрительный»: " + ", ".join(d["sources"]["suspicious"])
        )
    for old, new in safety.snapshots_before_masking(p.sources.sources).items():
        lines.append("Внимание: " + safety.stale_snapshot_note(old, new))
    if d["sources"]["closed_unconfirmed"]:
        lines.append(
            "Внимание: признаки закрытых данных не подтверждены человеком: "
            + ", ".join(d["sources"]["closed_unconfirmed"])
        )
    from strateg import classify  # импорт здесь: classify импортирует этот модуль

    d["claims"]["verdicts"] = classify.full_summary(p)  # с основаниями без вердикта: только полный status
    lines += [f"Внимание: {x}" for x in classify.summary_lines(d["claims"]["verdicts"])]
    if d["waiting_gates"]:
        lines.insert(
            0, "Ждём ответа на " + ", ".join(d["waiting_gates"]) + ": следующее сообщение человека — ответ"
        )
    elif d["paused"] == proj.PAUSE_STOP:
        lines.insert(0, "Ждём ответ человека на внеплановую остановку: следующее сообщение человека — ответ")
    lines.append(f"Дальше: {d['next_action'] or 'не задано'}")
    return emit(args, d, lines)
