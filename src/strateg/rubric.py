"""Рубрика полноты как исполнимая проверка: команды `strateg rubric check` и `strateg rubric record`.

Часть А — признаки, которые проверяет код (`strateg.rubric_checks`). Часть Б — вопросы-суждения, на
которые отвечает критик; CLI принимает ответы по `answer_format`, сам прогоняет часть А на текущем шаге,
выносит итог по `pass_rule` и пишет `completeness.yaml` и событие журнала (ADR-0006, ADR-0007). Отчёт о
прогоне берёт итог из этого файла, а не из памяти модели.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from strateg import project as proj
from strateg import storage
from strateg.commands import _norm_step, _open, _read_input
from strateg.models import (
    SCHEMA_VERSION,
    CompletenessFile,
    PartAResult,
    PartBAnswer,
    Step,
    Text,
    latin_hint,
)
from strateg.project import CliError, Project, UsageError
from strateg.project_check import COMPLETENESS_FILE, _format_errors
from strateg.rubric_checks import CHECKS, STEP_INDEX, Ctx, Result, knowledge_dir

RUBRIC_FILE = "completeness.yaml"
FOR_QUESTION = "Если"  # вопрос или его часть с этого слова может получить «да», когда условия нет
NO_CONDITION = "условия нет"
# «Если» в начале вопроса или в начале предложения внутри него.
_IF_PART = re.compile(rf"(?:^|[.?!]\s+){FOR_QUESTION}\b")
# Номер просмотренной записи: У-1, ВП-2, Вар-3 (русские буквы).
_RECORD_NO = re.compile(r"[А-Яа-яЁё]+-\d+")
CHECKED_BY = ("критик", "агент")
FIRST_FULL_STEP = "6"  # проверка полноты — шаг 6 (ADR-0006): раньше итог «пройдено» не выносится


# ---------------------------------------------------------------------------
# Рубрика
# ---------------------------------------------------------------------------


class _Rubric(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RubricA(_Rubric):
    id: Text
    step: Step
    file: list[Text]
    rule: Text
    source: Text
    cli_check: Text


class RubricB(_Rubric):
    id: Text
    step: Step
    question: Text
    where: list[Text]
    no_means: Text
    source: Text
    critical: bool


class Rubric(_Rubric):
    pass_rule: Text
    answer_format: Text
    how_to_read: Text
    part_a: list[RubricA] = Field(min_length=1)
    part_b: list[RubricB] = Field(min_length=1)


def load_rubric(path: Path | None = None) -> Rubric:
    """Рубрика полноты из `knowledge/rubrics/completeness.yaml`; нет файла или он не читается — `CliError`."""
    path = path or knowledge_dir() / "rubrics" / RUBRIC_FILE
    try:
        return Rubric.model_validate(yaml.safe_load(path.read_bytes().decode("utf-8-sig")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CliError(f"рубрика {path.name} не читается: {type(exc).__name__}") from exc
    except ValidationError as exc:
        raise CliError(f"рубрика {path.name} не по форме", _format_errors(path.name, (), {}, exc)) from exc


def verdict(rubric: Rubric, part_a: list[PartAResult], part_b: list[PartBAnswer]) -> str:
    """Итог по `pass_rule`: все признаки А «да», все критичные Б «да», некритичных «нет» не больше одного."""
    critical = {q.id for q in rubric.part_b if q.critical}
    a_ok = all(r.answer == "да" for r in part_a)
    b_critical_ok = all(r.answer == "да" for r in part_b if r.id in critical)
    soft_no = sum(1 for r in part_b if r.id not in critical and r.answer == "нет")
    return "пройдено" if a_ok and b_critical_ok and soft_no <= 1 else "не пройдено"


def due_checks(rubric: Rubric, step: str) -> list[RubricA]:
    """Признаки части А, которые к шагу уже должны выполняться (на шаге 6 — без признаков шага 7)."""
    here = STEP_INDEX[step]
    return [a for a in rubric.part_a if STEP_INDEX[a.step] <= here]


def run_checks(root: Path, rubric: Rubric, step: str) -> list[Result]:
    """Прогон признаков части А. Падение проверки не обрывает прогон: признак получает «нет»."""
    ctx = Ctx(root, step)
    results: list[Result] = []
    for a in due_checks(rubric, step):
        fn = CHECKS.get(a.cli_check)
        res = Result(a.id, a.cli_check, a.step)
        if fn is None:
            res.problems = [f"проверки {a.cli_check} нет в реестре: рубрика новее кода"]
        else:
            try:
                res.problems = fn(ctx)
            except Exception as exc:  # noqa: BLE001 — одна упавшая проверка не должна ронять прогон
                res.problems = [f"проверка не выполнена ({type(exc).__name__}): запустите `strateg check`"]
        results.append(res)
    return results


# ---------------------------------------------------------------------------
# strateg rubric check
# ---------------------------------------------------------------------------


def _check_lines(step: str, results: list[Result]) -> list[str]:
    lines = [f"Проверка полноты, часть А, шаг {step}: признаков {len(results)}"]
    for r in results:
        lines.append(f"{r.id}  {r.check}  {'да' if r.ok else 'нет'}")
        lines += [f"    - {p}" for p in r.problems]
    no = [r.id for r in results if not r.ok]
    lines.append(
        f"Итого: да — {len(results) - len(no)}, нет — {len(no)}" + (f" ({', '.join(no)})" if no else "")
    )
    return lines


def _result_data(r: Result) -> dict[str, Any]:
    return {
        "id": r.id,
        "check": r.check,
        "step": r.step,
        "answer": "да" if r.ok else "нет",
        "problems": r.problems,
        "message": r.message,
    }


def cmd_rubric_check(args: argparse.Namespace) -> int:
    p = _open(args)
    rubric = load_rubric()
    step = _norm_step(args.step) if getattr(args, "step", None) else p.state.step
    results = run_checks(p.root, rubric, step)
    no = [r.id for r in results if not r.ok]
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"ok": not no, "step": step, "checks": [_result_data(r) for r in results], "failed": no},
                ensure_ascii=False,
            )
        )
    else:
        for line in _check_lines(step, results):
            print(line)
    return 1 if no else 0


# ---------------------------------------------------------------------------
# strateg rubric record
# ---------------------------------------------------------------------------

_KEYS = {"id", "answer", "where", "why"}


def parse_answers(raw: Any, rubric: Rubric) -> list[PartBAnswer]:
    """Ответы части Б по `answer_format`: по одному на каждый вопрос, `answer` — «да» или «нет»,
    `where` и `why` непусты. Ошибки называют номер вопроса; любая ошибка — `CliError` без записи."""
    if not isinstance(raw, list) or not raw:
        raise CliError(
            "вход ответов части Б — непустой список записей {id, answer, where, why}",
            ["пример: - {id: Б-1, answer: да, where: plan.yaml, why: ветки ведут к разным действиям}"],
        )
    questions = {q.id: q for q in rubric.part_b}
    errors: list[str] = []
    answers: dict[str, PartBAnswer] = {}
    for n, item in enumerate(raw, start=1):
        label = f"запись {n}"
        if not isinstance(item, dict):
            errors.append(f"{label}: нужен набор полей id, answer, where, why")
            continue
        qid = item.get("id")
        if isinstance(qid, str):
            label = f"{qid}"
        extra = sorted(set(item) - _KEYS)
        if extra:
            errors.append(
                f"{label}: неизвестные поля {', '.join(map(str, extra))}: допустимы id, answer, where, why"
            )
        if not isinstance(qid, str) or qid not in questions:
            hint = latin_hint(qid) if isinstance(qid, str) else ""
            errors.append(f"запись {n}: вопроса {qid!r} нет в рубрике{hint}")
            continue
        question = questions[qid]
        before = len(errors)
        answer = item.get("answer")
        norm_answer = answer.strip().lower() if isinstance(answer, str) else None
        if norm_answer == "не знаю":
            errors.append(f"{label}: ответа «не знаю» нет: нечего проверить — значит «нет»")
        elif norm_answer not in ("да", "нет"):
            errors.append(f"{label}: answer — «да» или «нет» (словом, в кавычках, если YAML читает иначе)")
        for key in ("where", "why"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"{label}: {key} пусто: назовите "
                    + ("файл и место" if key == "where" else "причину одной строкой")
                )
        why = item.get("why")
        if norm_answer == "да" and isinstance(why, str) and NO_CONDITION in why.casefold():
            if not _IF_PART.search(question.question):
                errors.append(
                    f"{label}: «{NO_CONDITION}» допустимо только для вопроса или его части, "
                    f"которые начинаются со слова «{FOR_QUESTION}»"
                )
            elif not _RECORD_NO.search(why):
                errors.append(
                    f"{label}: к «{NO_CONDITION}» нужны номера просмотренных записей в why "
                    "(например У-1, ВП-2): без них проверку не повторить"
                )
        if qid in answers:
            errors.append(f"{label}: на вопрос больше одного ответа: нужен один")
        if len(errors) == before and qid not in answers:
            answers[qid] = PartBAnswer(
                id=qid,
                answer=norm_answer,
                where=item["where"].strip(),
                why=item["why"].strip(),  # type: ignore[arg-type]
            )
    errors += [
        f"{q}: нет ответа"
        for q in questions
        if q not in answers and not any(e.startswith(f"{q}:") for e in errors)
    ]
    if errors:
        raise CliError("ответы части Б не записаны: вход не по формату", errors)
    return [answers[q.id] for q in rubric.part_b]


def _same(a: CompletenessFile, b: CompletenessFile) -> bool:
    drop = {"checked_at"}
    return a.model_dump(mode="json", exclude=drop) == b.model_dump(mode="json", exclude=drop)


def _build(
    p: Project, rubric: Rubric, results: list[Result], answers: list[PartBAnswer], by: str
) -> CompletenessFile:
    part_a = [PartAResult(id=r.id, answer="да" if r.ok else "нет", message=r.message) for r in results]
    failed = [r.id for r in (*part_a, *answers) if r.answer == "нет"]
    return CompletenessFile(
        schema_version=SCHEMA_VERSION,
        step=p.state.step,
        checked_at=proj.now(),
        by=by,  # type: ignore[arg-type]
        part_a=part_a,
        part_b=answers,
        result=verdict(rubric, part_a, answers),  # type: ignore[arg-type]
        failed=failed,
    )


def _history_path(p: Project) -> str:
    taken = 0
    folder = p.root / proj.HISTORY_DIR
    if folder.is_dir():
        for path in folder.rglob("*"):
            source = proj.history_source(path.relative_to(p.root).as_posix()) if path.is_file() else None
            if source is not None and source[0] == COMPLETENESS_FILE:
                taken = max(taken, source[1])
    return proj.history_path(COMPLETENESS_FILE, taken + 1)


def _emit_record(args: argparse.Namespace, data: dict[str, Any], lines: list[str]) -> int:
    """Вывод `rubric record`: код 0 — «пройдено», 1 — «не пройдено»; `ok` в JSON — то же самое."""
    passed = data["result"] == "пройдено"
    if getattr(args, "json", False):
        print(json.dumps({"ok": passed, **data}, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return 0 if passed else 1


def cmd_rubric_record(args: argparse.Namespace) -> int:
    by = getattr(args, "by", None)
    if by not in CHECKED_BY:
        raise UsageError(
            "не указано, кто проверял: задайте --by критик (проверка независимая, ответы дал критик) "
            "или --by агент (ответы дал сам агент, проверка не независимая)",
            ["значения по умолчанию нет: иначе проверка, сделанная самим агентом, пройдёт как независимая"],
        )
    p = _open(args)
    if STEP_INDEX[p.state.step] < STEP_INDEX[FIRST_FULL_STEP]:
        raise UsageError(
            f"проверка полноты — шаг {FIRST_FULL_STEP}, а проект на шаге {p.state.step}: "
            "до шага 6 часть А проверена не полностью и итог «пройдено» не выносится; "
            f"сверить файлы до шага 6 можно командой `strateg rubric check --step {p.state.step}`"
        )
    rubric = load_rubric()
    try:
        answers = parse_answers(_read_input(args.file), rubric)
    except UsageError:
        raise
    except CliError as err:  # вход не по формату — ошибка входа (код 2), а не итог проверки
        raise UsageError(err.message, err.details) from err
    started = time.monotonic()
    results = run_checks(p.root, rubric, p.state.step)
    new = _build(p, rubric, results, answers, by)

    path = p.root / COMPLETENESS_FILE
    old_bytes = path.read_bytes() if path.is_file() else None
    old: CompletenessFile | None = None
    if old_bytes is not None:
        try:
            old = CompletenessFile.model_validate(proj.read_yaml(path), by_name=False)
        except (CliError, ValidationError):
            old = None
    data = _record_data(rubric, new)
    if old is not None and _same(old, new):
        return _emit_record(
            args,
            {"changed": False, **data},
            [f"{COMPLETENESS_FILE}: без изменений", *_summary_lines(rubric, new)],
        )

    text = proj.dump_yaml(new)
    history = _history_path(p) if old_bytes is not None else None
    runs = sum(1 for e in p.journal if e.kind == "действие" and e.name == "strateg rubric record")
    with proj.transaction(p.root):
        if history is not None and old_bytes is not None:
            dest = p.root / history
            dest.parent.mkdir(parents=True, exist_ok=True)
            storage.atomic_write_bytes(dest, old_bytes)
        proj.write_text(path, text)
        p.add_event(
            p.event(
                kind="действие",
                name="strateg rubric record",
                idempotency_hash=proj.key_hash("rubric", runs + 1, proj.sha256_bytes(text.encode("utf-8"))),
                status="выполнено",
                duration_s=round(time.monotonic() - started, 2),
                message=(
                    f"Проверка полноты: {new.result}; нет: {', '.join(new.failed) or '—'}; проверял: {by}"
                ),
            )
        )
        p.save_state()
    lines = [
        f"{COMPLETENESS_FILE}: "
        + ("создан" if history is None else f"перезаписан, прежняя версия в {history}"),
        *_summary_lines(rubric, new),
    ]
    return _emit_record(args, {"changed": True, "history": history, **data}, lines)


def _record_data(rubric: Rubric, comp: CompletenessFile) -> dict[str, Any]:
    no_means = {q.id: q.no_means for q in rubric.part_b if q.id in comp.failed}
    return {
        "result": comp.result,
        "by": comp.by,
        "independent": comp.by == "критик",
        "step": comp.step,
        "failed": comp.failed,
        "part_a": [r.model_dump(mode="json", exclude_none=True) for r in comp.part_a],
        "part_b_failed": no_means,
        "file": COMPLETENESS_FILE,
    }


def _summary_lines(rubric: Rubric, comp: CompletenessFile) -> list[str]:
    a_no = [r for r in comp.part_a if r.answer == "нет"]
    lines = [
        f"Проверка полноты: {comp.result} (шаг {comp.step}, проверял: {comp.by}).",
        f"Часть А: «да» — {len(comp.part_a) - len(a_no)} из {len(comp.part_a)}.",
    ]
    if comp.by == "агент":
        lines.append("Проверка не независимая: ответы части Б дал сам агент (в файле by: агент).")
    for r in a_no:
        lines.append(f"  {r.id}: нет — {r.message}")
    means = {q.id: q.no_means for q in rubric.part_b}
    for r in comp.part_b:
        if r.answer == "нет":
            lines.append(f"  {r.id}: нет — {r.why}. Что дополнить: {means[r.id]}")
    if comp.result == "не пройдено":
        lines.append("В доработку идёт каждый признак с «нет».")
    return lines
