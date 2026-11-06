"""Файлы выводов проекта: рамка, план, пробелы, варианты, обещания, план проверки, решения, версии.

Пример — `tests/fixtures/project_valid/`. Каждый отрицательный пример портит копию примера и ждёт русскую
ошибку с файлом и номером записи. Ошибки в консоль не печатаются (консоль Windows бывает в cp1251).
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from strateg import models
from strateg.models import (
    FrameFile,
    GapsFile,
    OptionsFile,
    PlanFile,
    VerificationFile,
)
from strateg.project import _tracked_files, dump_yaml, history_path, history_source
from strateg.project_check import OUTPUT_FILES, check_project
from test_project_check import FIXTURE, assert_error, edit_jsonl, edit_yaml, errors_of

NEW_FILES = ("frame.yaml", "plan.yaml", "gaps.yaml", "options.yaml", "promises.yaml")
NEW_FILES += ("verification.yaml", "decisions.yaml")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    return dest


def put(path: Path, text: str) -> None:
    """Запись UTF-8 с окончаниями LF: `write_text` на Windows писал бы CRLF."""
    path.write_bytes(text.encode("utf-8"))


def load(project: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load((project / name).read_text(encoding="utf-8"))


def option(d: dict[str, Any], oid: str) -> dict[str, Any]:
    return next(o for o in d["options"] if o["id"] == oid)


def synthetic_claim() -> dict[str, Any]:
    return {
        "id": "У-7",
        "text": "Модельный респондент назвал защиту нужной",
        "type": "синтетика",
        "origin": "синтетика",
        "step": "2б",
        "attempt": 1,
        "subject": "ответ модельного респондента",
        "status": "действует",
        "status_history": [{"status": "действует", "at": "2026-11-06T10:50:00+03:00", "by": "агент"}],
    }


# --- корректный пример ----------------------------


def test_example_with_all_files_is_valid():
    assert check_project(FIXTURE) == []
    assert all((FIXTURE / n).is_file() for n in OUTPUT_FILES)


def test_project_without_new_files_is_valid(project):
    for name in NEW_FILES:
        (project / name).unlink()
    shutil.rmtree(project / "history")
    shutil.rmtree(project / "outputs")
    assert errors_of(project) == []


def test_history_version_of_example_is_a_valid_frame():
    data = yaml.safe_load((FIXTURE / "history" / "frame.v1.yaml").read_text(encoding="utf-8"))
    assert FrameFile.model_validate(data).version == 1


def test_files_are_dumped_with_keys_if_and_from(project):
    plan = PlanFile.model_validate(load(project, "plan.yaml"))
    text = dump_yaml(plan)
    assert "- if:" in text and "if_" not in text
    assert PlanFile.model_validate(yaml.safe_load(text)) == plan
    options = OptionsFile.model_validate(load(project, "options.yaml"))
    text = dump_yaml(options)
    assert "from: Вар-" in text and "from_:" not in text
    assert OptionsFile.model_validate(yaml.safe_load(text)) == options


def test_json_schemas_use_the_keys_of_the_files():
    plan = models.PlanFile.model_json_schema()
    assert set(plan["$defs"]["Branch"]["properties"]) == {"if", "then"}
    options = models.OptionsFile.model_json_schema()
    assert set(options["$defs"]["Differs"]["properties"]) == {"axis", "from", "how"}


# --- рамка и событие В1 ----------------------------


def test_frame_decision_space_needs_two_values(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(decision_space=["продавать отдельно"]))
    assert_error(project, "frame.yaml", "decision_space", "нужно не меньше 2")


def test_frame_decision_space_values_differ(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(decision_space=["Тарифы", "тарифы "]))
    assert_error(project, "frame.yaml", "повторы")


def test_frame_field_needs_origin_from_the_list(project):
    edit_yaml(project, "frame.yaml", lambda d: d["deadline"].update(origin="придумано"))
    assert_error(project, "frame.yaml", "deadline.origin", "допустимо: «из запроса», «из материалов»")


def test_frame_source_only_for_materials(project):
    edit_yaml(project, "frame.yaml", lambda d: d["deadline"].update(source="И-1"))
    assert_error(project, "frame.yaml", "deadline", "только у «из материалов»")


def test_frame_source_must_exist(project):
    edit_yaml(project, "frame.yaml", lambda d: d["data"].update(source="И-9"))
    assert_error(project, "frame.yaml", "data.source", "несуществующий источник И-9")


def test_frame_field_without_value_is_rejected(project):
    edit_yaml(project, "frame.yaml", lambda d: d["region"].pop("value"))
    assert_error(project, "frame.yaml", "region.value", "нет обязательного поля")


def test_frame_version_must_be_integer_from_one(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=0))
    assert_error(project, "frame.yaml", "version")
    edit_yaml(project, "frame.yaml", lambda d: d.update(version="2"))
    assert_error(project, "frame.yaml", "version", "ожидается целое число")


def test_frame_in_gate_b2_is_rejected(project):
    frame = load(project, "frame.yaml")
    frame.pop("schema_version"), frame.pop("version")

    def add(rows: list[dict[str, Any]]) -> None:
        gate = next(r for r in rows if r.get("gate", {}).get("gate") == "В2")["gate"]
        gate["frame"] = frame

    edit_jsonl(project, "journal.jsonl", add)
    assert_error(project, "journal.jsonl", "строка 4", "frame бывает только у ворот В1")


def test_answered_b1_needs_frame_when_frame_file_exists(project):
    def drop(rows: list[dict[str, Any]]) -> None:
        next(r for r in rows if r.get("gate", {}).get("gate") == "В1")["gate"].pop("frame")

    edit_jsonl(project, "journal.jsonl", drop)
    assert_error(project, "journal.jsonl", "строка 2", "у отвеченного В1 нет поля frame")
    (project / "frame.yaml").unlink()
    shutil.rmtree(project / "history")
    assert errors_of(project) == []


def test_frame_in_b1_event_checks_its_sources(project):
    def change(rows: list[dict[str, Any]]) -> None:
        gate = next(r for r in rows if r.get("gate", {}).get("gate") == "В1")["gate"]
        gate["frame"]["data"]["source"] = "И-9"

    edit_jsonl(project, "journal.jsonl", change)
    assert_error(project, "journal.jsonl", "строка 2", "gate.frame.data.source", "И-9")


def test_frame_in_b1_event_is_checked_like_the_file(project):
    def change(rows: list[dict[str, Any]]) -> None:
        gate = next(r for r in rows if r.get("gate", {}).get("gate") == "В1")["gate"]
        gate["frame"]["decision"]["origin"] = "придумано"

    edit_jsonl(project, "journal.jsonl", change)
    assert_error(project, "journal.jsonl", "строка 2", "gate.frame.decision.origin")


# --- версии history/ ----------------------------


def test_history_path_names():
    assert history_path("frame.yaml", 1) == "history/frame.v1.yaml"
    assert history_path("outputs/options.md", 2) == "history/outputs/options.v2.md"
    assert history_path("Makefile", 3) == "history/Makefile.v3"
    assert history_path("a.tar.gz", 1) == "history/a.tar.v1.gz"


@pytest.mark.parametrize(
    ("rel", "version"),
    [
        ("../frame.yaml", 1),
        ("C:/frame.yaml", 1),
        ("frame.yaml", 0),
        ("frame.yaml", True),
        ("history/a.md", 1),
    ],
)
def test_history_path_rejects_bad_input(rel, version):
    with pytest.raises(ValueError):
        history_path(rel, version)


def test_history_source_is_the_inverse_of_history_path():
    for rel in ("frame.yaml", "outputs/options.md", "Makefile", "a.tar.gz", "x.v2.yaml"):
        assert history_source(history_path(rel, 7)) == (rel, 7)
    for bad in (
        "history/frame.yaml",
        "history/frame.v0.yaml",
        "history/frame.v01.yaml",
        "frame.v1.yaml",
        "history",
    ):
        assert history_source(bad) is None


def test_frame_version_needs_all_previous_versions(project):
    (project / "history" / "frame.v1.yaml").unlink()
    assert_error(project, "frame.yaml", "версия 2", "версии 1 нет", "history/frame.v1.yaml")


def test_frame_version_three_needs_versions_one_and_two(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=3))
    assert_error(project, "frame.yaml", "версия 3", "версии 2 нет")
    v2 = (
        (project / "history" / "frame.v1.yaml")
        .read_text(encoding="utf-8")
        .replace("\nversion: 1\n", "\nversion: 2\n")
    )
    put(project / "history" / "frame.v2.yaml", v2)
    assert errors_of(project) == []


def test_frame_version_one_needs_no_history(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=1))
    shutil.rmtree(project / "history")
    assert errors_of(project) == []


def test_history_names_must_follow_the_pattern(project):
    put(project / "history" / "frame.yaml", "x: 1\n")
    assert_error(project, "history/frame.yaml", "<файл>.v<N>.<расширение>")
    (project / "history" / "frame.yaml").unlink()
    (project / "history" / "outputs").mkdir()
    put(project / "history" / "outputs" / "options.v2.md", "# Варианты\n")
    assert errors_of(project) == []


def test_tracked_files_include_new_files_and_history():
    names = {p.relative_to(FIXTURE).as_posix() for p in _tracked_files(FIXTURE)}
    assert set(NEW_FILES) <= names
    assert "history/frame.v1.yaml" in names


# --- план ----------------------------


def test_plan_question_with_equal_then_is_rejected(project):
    edit_yaml(
        project,
        "plan.yaml",
        lambda d: d["questions"][0]["branches"][1].update(then="Продавать  ОТДЕЛЬНОЙ подпиской."),
    )
    assert_error(project, "plan.yaml", "запись ВП-1 (№1)", "одинаковое действие", "excluded")


def test_plan_question_needs_two_branches(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0]["branches"].pop())
    assert_error(project, "plan.yaml", "ВП-1", "branches", "нужно не меньше 2")


def test_plan_branch_keys_are_if_and_then(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0]["branches"][0].update(if_="x"))
    assert_error(project, "plan.yaml", "ВП-1", "branches[0].if_", "неизвестное поле")


def test_plan_gap_status_needs_gap_reference(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][2].pop("gap"))
    assert_error(project, "plan.yaml", "ВП-3", "«пробел» без ссылки на пробел")
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][2].update(status="открыт", gap="П-1"))
    assert_error(project, "plan.yaml", "ВП-3", "gap задано при статусе «открыт»")


def test_plan_gap_must_exist(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][2].update(gap="П-9"))
    assert_error(project, "plan.yaml", "ВП-3", "поле gap", "П-9", "gaps.yaml")


def test_plan_gap_needs_gaps_file(project):
    (project / "gaps.yaml").unlink()
    assert_error(project, "plan.yaml", "ВП-3", "поле gap", "файла gaps.yaml нет")


def test_plan_estimate_is_not_empty(project):
    edit_yaml(project, "plan.yaml", lambda d: d.update(estimate={}))
    assert_error(project, "plan.yaml", "estimate", "пуста")


def test_plan_close_cost_may_be_empty(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0].update(close_cost={}))
    assert errors_of(project) == []


def test_plan_impact_is_from_the_list(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0].update(impact="огромное"))
    assert_error(project, "plan.yaml", "ВП-1", "impact", "допустимо: «высокое», «среднее» или «низкое»")


def test_plan_question_of_claim_must_exist_in_plan(project):
    def change(d: dict[str, Any]) -> None:
        d["claims"][2]["plan_question"] = "ВП-9"

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "claims.yaml", "запись У-3 (№3)", "plan_question", "ВП-9", "plan.yaml")


def test_plan_question_with_latin_letter_is_explained(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(plan_question="B" + "П-2"))
    assert_error(project, "claims.yaml", "У-3", "plan_question", "латинские буквы", "«B»")


def test_free_plan_question_text_stays_valid(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(plan_question="Кто подключает защиту?"))
    assert errors_of(project) == []


def test_plan_question_is_not_checked_without_plan(project):
    (project / "plan.yaml").unlink()
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(plan_question="ВП-9"))
    assert [e for e in errors_of(project) if "claims.yaml" in e] == []


# --- пробелы и перечень показателей ----------------------------


def test_gap_needs_searched(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].pop("searched"))
    assert_error(project, "gaps.yaml", "запись П-1 (№1)", "searched", "нет обязательного поля")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(searched=[]))
    assert_error(project, "gaps.yaml", "П-1", "searched", "нужно не меньше 1")


def test_gap_search_date_needs_quotes(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0]["searched"][0].update(date=dt.date(2026, 11, 6)))
    assert_error(project, "gaps.yaml", "П-1", "searched[0].date", "возьмите его в кавычки")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0]["searched"][0].update(date="06.11.2026"))
    assert_error(project, "gaps.yaml", "П-1", "searched[0].date", "2026-11-06")


def test_gap_reason_other_needs_note(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(reason="другая"))
    assert_error(project, "gaps.yaml", "П-1", "«другая» без пояснения")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(reason_note="источник удалён"))
    assert errors_of(project) == []
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(reason="нет данных"))
    assert_error(project, "gaps.yaml", "П-1", "пояснение бывает только у «другая»")


def test_gap_reason_is_from_the_list(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(reason="лень"))
    assert_error(project, "gaps.yaml", "П-1", "reason", "допустимо: «нет данных», «данные закрыты»")


def test_closed_gap_needs_closing_claims(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт"))
    assert_error(project, "gaps.yaml", "П-1", "«закрыт» без ссылок на утверждения")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(closed_by=["У-2"]))
    assert errors_of(project) == []
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="открыт"))
    assert_error(project, "gaps.yaml", "П-1", "бывают только у «закрыт»")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт", closed_by=["У-99"]))
    assert_error(project, "gaps.yaml", "П-1", "closed_by", "несуществующее утверждение У-99")


def test_gap_assumption_must_be_an_assumption_claim(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(assumption="У-2"))
    assert_error(project, "gaps.yaml", "П-1", "assumption", "У-2 имеет тип «факт»", "«допущение»")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(assumption="У-1"))
    assert errors_of(project) == []  # прежнее допущение допустимо: оно заменено, но пробел его не держит


def test_gap_options_must_exist(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(affects_options=["Вар-9"]))
    assert_error(project, "gaps.yaml", "П-1", "affects_options", "Вар-9", "options.yaml")
    (project / "options.yaml").unlink()
    assert_error(project, "gaps.yaml", "П-1", "affects_options", "файла options.yaml нет")


def test_gap_options_may_be_empty_before_step_four(project):
    edit_yaml(project, "gaps.yaml", lambda d: [g.update(affects_options=[]) for g in d["gaps"]])
    for name in ("options.yaml", "decisions.yaml"):
        (project / name).unlink()
    assert [e for e in errors_of(project) if "gaps.yaml" in e] == []


def test_gap_hypothesis_must_be_a_hypothesis_claim(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(hypothesis="У-2"))
    assert_error(project, "gaps.yaml", "запись П-1 (№1)", "hypothesis", "У-2 имеет тип «факт»", "«гипотеза»")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(hypothesis="У-99"))
    assert_error(project, "gaps.yaml", "П-1", "hypothesis", "несуществующее утверждение У-99")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(hypothesis="У-3"))
    assert errors_of(project) == []


def test_gap_close_via_is_from_the_list(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(close_via="спросить у знакомого"))
    assert_error(
        project, "gaps.yaml", "П-1", "close_via", "допустимо: «открытый источник», «материал руководителя»"
    )
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(close_via="открытый источник"))
    assert errors_of(project) == []
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].pop("close_via"))
    assert errors_of(project) == []


def test_metric_coverage_has_exactly_one_answer(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(gap="П-1"))
    assert_error(
        project, "gaps.yaml", "metric_coverage", "запись №1", "«охват»", "несколько ответов", "claims, gap"
    )
    edit_yaml(
        project, "gaps.yaml", lambda d: [d["metric_coverage"][0].pop(k, None) for k in ("claims", "gap")]
    )
    assert_error(project, "gaps.yaml", "metric_coverage", "запись №1", "нет ответа")


def test_metric_coverage_kind_is_not_repeated(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"].append(dict(d["metric_coverage"][0])))
    assert_error(project, "gaps.yaml", "«охват»", "повторён", "записи №1 и №6")


def test_metric_coverage_kind_is_from_the_list(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(kind="настроение"))
    assert_error(project, "gaps.yaml", "metric_coverage", "kind", "допустимо: «охват», «конверсия»")


def test_metric_coverage_references_must_exist(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(claims=["У-99"]))
    assert_error(project, "gaps.yaml", "metric_coverage", "запись №1", "несуществующее утверждение У-99")
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][1].update(gap="П-9"))
    assert_error(project, "gaps.yaml", "metric_coverage", "запись №2", "П-9")


def test_metric_coverage_gap_agrees_with_gap_kind(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][1].update(gap="П-1"))
    assert_error(project, "gaps.yaml", "запись №2", "П-1", "«удержание и отток»", "«конверсия»")


def test_metric_coverage_may_be_absent_or_partial(project):
    edit_yaml(project, "gaps.yaml", lambda d: d.update(metric_coverage=d["metric_coverage"][:2]))
    assert errors_of(project) == []
    edit_yaml(project, "gaps.yaml", lambda d: d.pop("metric_coverage"))
    assert errors_of(project) == []


# --- варианты ----------------------------


def test_three_options_instead_of_four(project):
    edit_yaml(project, "options.yaml", lambda d: d["options"].pop())
    assert_error(project, "options.yaml", "вариантов 3", "не меньше четырёх")


def test_options_need_keep_as_is(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-1").update(kind="активный"))
    assert_error(project, "options.yaml", "нет варианта «оставить как есть»")


def test_options_need_close_or_do_not_launch(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-4").update(kind="активный"))
    assert_error(project, "options.yaml", "нет варианта «закрыть направление»")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-4").update(kind="не запускать"))
    assert errors_of(project) == []


def test_options_need_two_active(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: option(d, "Вар-3").update(kind="не запускать", return_condition="x"),
    )
    assert_error(project, "options.yaml", "активных вариантов 1")


def test_option_with_one_objection_is_rejected(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"].pop())
    assert_error(project, "options.yaml", "запись Вар-2 (№2)", "objections", "нужно не меньше 2")


def test_objection_basis_is_claim_or_hypothesis_word(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][0].update(basis="возможно"))
    assert_error(project, "options.yaml", "Вар-2", "objections[0].basis", "или слово «гипотеза»")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][0].update(basis="Y-3"))
    assert_error(project, "options.yaml", "Вар-2", "objections[0].basis", "латинские буквы", "«Y»")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][0].update(basis="У-99"))
    assert_error(project, "options.yaml", "Вар-2", "objections[0].basis", "несуществующее утверждение У-99")


def test_effect_with_text_needs_forecast(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["comparison"]["effect"].pop("forecast"))
    assert_error(project, "options.yaml", "запись Вар-2 (№2)", "comparison.effect", "нет ссылки на прогноз")
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-2")["comparison"]["first_signal"].pop("forecast")
    )
    assert_error(project, "options.yaml", "Вар-2", "comparison.first_signal", "нет ссылки на прогноз")


def test_forecast_must_be_a_forecast_claim(project):
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-2")["comparison"]["effect"].update(forecast="У-2")
    )
    assert_error(
        project, "options.yaml", "Вар-2", "comparison.effect.forecast", "У-2 имеет тип «факт»", "«прогноз»"
    )
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-2")["comparison"]["effect"].update(forecast="У-3")
    )
    assert_error(project, "options.yaml", "Вар-2", "comparison.effect.forecast", "«гипотеза»")


def test_empty_cell_needs_reason_and_no_forecast(project):
    def both(d: dict[str, Any]) -> None:
        option(d, "Вар-1")["comparison"]["cost"]["why_empty"] = "нет данных"

    edit_yaml(project, "options.yaml", both)
    assert_error(project, "options.yaml", "Вар-1", "comparison.cost", "ровно одно из text и why_empty")

    def neither(d: dict[str, Any]) -> None:
        option(d, "Вар-1")["comparison"]["risk"] = {}

    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-1")["comparison"]["cost"].pop("why_empty"))
    edit_yaml(project, "options.yaml", neither)
    assert_error(project, "options.yaml", "Вар-1", "comparison.risk", "ровно одно из text и why_empty")


def test_empty_forecast_cell_must_not_have_forecast(project):
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-1")["comparison"]["effect"].update(forecast="У-4")
    )
    assert_error(project, "options.yaml", "Вар-1", "comparison.effect", "forecast задан у пустой ячейки")


def test_reversibility_with_text_needs_mechanism_time_price(project):
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-2")["comparison"]["reversibility"].pop("what")
    )
    assert_error(project, "options.yaml", "Вар-2", "comparison.reversibility", "нет: what")
    edit_yaml(
        project,
        "options.yaml",
        lambda d: [option(d, "Вар-2")["comparison"]["reversibility"].pop(k, None) for k in ("time", "price")],
    )
    assert_error(project, "options.yaml", "Вар-2", "comparison.reversibility", "what, time, price")


def test_keep_as_is_needs_inaction_cost(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-1").pop("inaction_cost"))
    assert_error(project, "options.yaml", "Вар-1", "нет цены бездействия")


def test_closing_needs_return_condition(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-4").pop("return_condition"))
    assert_error(project, "options.yaml", "Вар-4", "«закрыть направление»", "условия возврата")


def test_option_differs_from_another_option(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["differs"].update({"from": "Вар-2"}))
    assert_error(project, "options.yaml", "Вар-2", "отличается сам от себя")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["differs"].update({"from": "Вар-9"}))
    assert_error(project, "options.yaml", "Вар-2", "differs.from", "Вар-9")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["differs"].update(axis="масштаб"))
    assert_error(
        project,
        "options.yaml",
        "Вар-2",
        "differs.axis",
        "допустимо: «аудитория», «ценность» или «модель дохода»",
    )


def test_option_number_with_latin_letter_is_explained(project):
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2").update(id="B" + "ар-2"))
    errors = assert_error(project, "options.yaml", "латинские буквы", "«B»")
    assert any("Вар-1 (буквы русские)" in e for e in errors)


def test_option_ids_are_not_repeated(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: option(d, "Вар-3").update(
            id="Вар-2", differs={**option(d, "Вар-3")["differs"], "from": "Вар-1"}
        ),
    )
    assert_error(project, "options.yaml", "номер Вар-2 встречается больше одного раза")


def test_recommendation_rules(project):
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(change_mind=[]))
    assert_error(project, "options.yaml", "recommendation", "change_mind", "нужно не меньше 1")
    edit_yaml(
        project,
        "options.yaml",
        lambda d: d["recommendation"].update(
            change_mind=[{"metric": "m", "threshold": "ниже 5", "action": "a"}], loses_on="цвет"
        ),
    )
    assert_error(project, "options.yaml", "loses_on", "допустимо: «эффект», «затраты», «риск»")
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(loses_on="риск"))
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(option="Вар-9"))
    assert_error(project, "options.yaml", "recommendation", "option", "Вар-9")


def test_chosen_must_exist_and_may_be_empty(project):
    edit_yaml(project, "options.yaml", lambda d: d.update(chosen="Вар-9"))
    assert_error(project, "options.yaml", "chosen", "Вар-9")
    edit_yaml(project, "options.yaml", lambda d: d.pop("chosen"))
    assert errors_of(project) == []


def test_recommendation_cannot_stand_on_replaced_claim(project):
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-1"]))
    assert_error(
        project, "options.yaml", "recommendation", "claims", "У-1", "статус «заменено»", "рекомендацию"
    )


def test_other_references_may_use_replaced_claim(project):
    """Прежняя версия может опираться на заменённое утверждение: ошибка только у рекомендации и обещания."""
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][1].update(basis="У-1"))
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["basis"].append("У-1"))
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт", closed_by=["У-1"]))
    assert errors_of(project) == []


# --- обещания ----------------------------


def test_confirmed_promise_without_facts_is_rejected(project):
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(claims=[]))
    assert_error(project, "promises.yaml", "запись О-1 (№1)", "«подтверждено» без фактов")
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(status="подтверждено частично"))
    assert_error(project, "promises.yaml", "О-1", "«подтверждено частично» без фактов")


def test_unconfirmed_promise_may_have_no_facts(project):
    assert load(project, "promises.yaml")["promises"][1]["status"] == "не подтверждено"
    assert errors_of(project) == []


def test_promise_on_hypothesis_is_rejected(project):
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(claims=["У-3"]))
    assert_error(project, "promises.yaml", "запись О-1 (№1)", "claims", "У-3 имеет тип «гипотеза»", "«факт»")


def test_promise_cannot_stand_on_replaced_claim(project):
    def change(d: dict[str, Any]) -> None:
        d["claims"][1]["status"] = "отозвано"
        d["claims"][1]["status_history"].append(
            {"status": "отозвано", "at": "2026-11-06T11:00:00+03:00", "by": "пользователь"}
        )

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "promises.yaml", "О-1", "claims", "У-2", "статус «отозвано»", "обещание")


def test_promise_status_is_from_the_list(project):
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(status="по данным компании"))
    assert_error(project, "promises.yaml", "О-1", "status", "допустимо: «подтверждено»")


def test_promise_number_with_latin_o_is_explained(project):
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(id="O-1"))
    assert_error(project, "promises.yaml", "O-1", "номер обещания вида О-1", "латинские буквы", "«O»")


def test_promise_mark_in_comms_strategy_must_exist(project):
    path = project / "outputs" / "comms-strategy.md"
    put(path, path.read_text(encoding="utf-8") + "Ещё одно обещание [О-9].\n")
    assert_error(project, "outputs/comms-strategy.md", "[О-9]", "не ведёт на обещание")


def test_promise_mark_with_latin_letter_is_explained(project):
    path = project / "outputs" / "comms-strategy.md"
    put(path, path.read_text(encoding="utf-8").replace("[О-1]", "[O-1]"))
    assert_error(project, "outputs/comms-strategy.md", "[O-1]", "латинской буквой O", "русская О")


def test_promise_marks_need_promises_file(project):
    (project / "promises.yaml").unlink()
    assert_error(project, "outputs/comms-strategy.md", "[О-1]", "файла promises.yaml нет")


def test_comms_strategy_without_marks_or_file_is_fine(project):
    put(project / "outputs" / "comms-strategy.md", "# Стратегия\n")
    assert errors_of(project) == []
    (project / "outputs" / "comms-strategy.md").unlink()
    (project / "promises.yaml").unlink()
    assert errors_of(project) == []


# --- план проверки ----------------------------


def test_metric_on_synthetic_is_rejected(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"].append(synthetic_claim()))
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(hypothesis="У-7"))
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "hypothesis", "У-7 — синтетика")
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][0].update(claim="У-7"))
    assert_error(project, "verification.yaml", "premortem", "запись №1", "claim", "У-7 — синтетика")


def test_row_hypothesis_must_be_a_hypothesis(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(hypothesis="У-4"))
    assert_error(project, "verification.yaml", "ПП-1", "hypothesis", "У-4 имеет тип «прогноз»", "«гипотеза»")


def test_main_row_needs_hypothesis_but_guard_row_may_go_without(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].pop("hypothesis"))
    assert_error(project, "verification.yaml", "ПП-1", "у основного показателя нет гипотезы")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(hypothesis="У-3", kind="защитный"))
    assert errors_of(project) == []


@pytest.mark.parametrize("owner", ["команда", "Команда.", "все", "TBD", "tbd"])
def test_owner_must_be_a_role(project, owner):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(owner=owner))
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "owner", "не роль")


def test_decision_and_commitment_owner_must_be_a_role(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(owner="команда"))
    assert_error(project, "decisions.yaml", "запись Реш-1 (№1)", "owner", "не роль")
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(owner="руководитель направления"))
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["commitments"][0].update(owner="все"))
    assert_error(project, "decisions.yaml", "Реш-1", "commitments[0].owner", "не роль")


def test_row_with_method_needs_strength_and_limits(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].pop("strength"))
    assert_error(project, "verification.yaml", "ПП-1", "у метода нет оценки силы")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(strength="слабая"))
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].pop("not_prove"))
    assert_error(project, "verification.yaml", "ПП-1", "не докажет")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(strength="огромная", not_prove="x"))
    assert_error(
        project, "verification.yaml", "ПП-1", "strength", "допустимо: «сильная», «средняя» или «слабая»"
    )


def test_row_without_method_may_omit_strength(project):
    assert "method" not in load(project, "verification.yaml")["rows"][1]
    assert errors_of(project) == []


def test_row_magnitude_range(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0]["magnitude"].update(low=70))
    assert_error(project, "verification.yaml", "ПП-1", "magnitude", "low больше high")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0]["magnitude"].update(low="50", high=60))
    assert_error(project, "verification.yaml", "ПП-1", "magnitude.low", "ожидается число")


def test_row_needed_n_is_positive_integer(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(needed_n=0))
    assert_error(project, "verification.yaml", "ПП-1", "needed_n")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(needed_n=30))
    assert errors_of(project) == []


def test_row_without_change_mind_threshold_is_rejected(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0]["change_mind"].pop("threshold"))
    assert_error(project, "verification.yaml", "ПП-1", "change_mind.threshold", "нет обязательного поля")


def test_row_baseline_may_point_to_a_gap(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline="П-9"))
    assert_error(project, "verification.yaml", "ПП-1", "baseline", "П-9", "gaps.yaml")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline="2 % в месяц"))
    assert errors_of(project) == []


def test_premortem_has_exactly_three_causes(project):
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"].pop())
    assert_error(project, "verification.yaml", "причин пре-мортема 2", "нужно ровно 3")
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"].extend([dict(d["premortem"][0])] * 2))
    assert_error(project, "verification.yaml", "причин пре-мортема 4", "нужно ровно 3")


def test_premortem_cause_must_be_a_hypothesis_with_a_row(project):
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][0].update(claim="У-2"))
    assert_error(project, "verification.yaml", "premortem", "запись №1", "У-2 имеет тип «факт»")
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][1].update(row="ПП-9"))
    assert_error(project, "verification.yaml", "premortem", "запись №2", "row", "ПП-9")
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][2].update(hits_choice="нет"))
    assert_error(project, "verification.yaml", "premortem", "запись №3", "hits_choice", "логическое значение")


def test_undesired_effect_row_must_exist(project):
    edit_yaml(project, "verification.yaml", lambda d: d["undesired_effects"][0].update(row="ПП-9"))
    assert_error(project, "verification.yaml", "undesired_effects", "запись №1", "ПП-9")


def test_row_ids_are_not_repeated(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][1].update(id="ПП-1"))
    assert_error(project, "verification.yaml", "номер ПП-1 встречается больше одного раза")


def test_from_change_mind_must_be_a_record_of_the_recommendation(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4].update(from_change_mind=2))
    assert_error(
        project, "verification.yaml", "запись ПП-5 (№5)", "from_change_mind", "записи 2 нет", "там записей: 1"
    )
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4].update(from_change_mind=0))
    assert_error(project, "verification.yaml", "ПП-5", "from_change_mind")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4].update(from_change_mind="1"))
    assert_error(project, "verification.yaml", "ПП-5", "from_change_mind", "ожидается целое число")
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4].update(from_change_mind=1))
    assert errors_of(project) == []


def test_from_change_mind_is_not_checked_without_options(project):
    for name in ("options.yaml", "decisions.yaml"):
        (project / name).unlink()
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4].update(from_change_mind=7))
    assert not [e for e in errors_of(project) if "from_change_mind" in e]


# --- решения ----------------------------


def test_decision_condition_needs_threshold(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].pop("threshold"))
    assert_error(
        project, "decisions.yaml", "запись Реш-1 (№1)", "conditions[0].threshold", "нет обязательного поля"
    )
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(threshold=" "))
    assert_error(project, "decisions.yaml", "Реш-1", "conditions[0].threshold", "строка пустая")


def test_decision_condition_needs_date(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][1].pop("date"))
    assert_error(
        project, "decisions.yaml", "запись Реш-1 (№1)", "conditions[1].date", "нет обязательного поля"
    )


def test_decision_needs_conditions(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(conditions=[]))
    assert_error(project, "decisions.yaml", "Реш-1", "conditions", "нужно не меньше 1")


def test_decision_condition_origin_is_from_the_list(project):
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(origin="по настроению")
    )
    assert_error(
        project, "decisions.yaml", "Реш-1", "origin", "допустимо: «передумаю, если» или «пре-мортем»"
    )


def test_dates_without_quotes_are_explained(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(review_date=dt.date(2027, 1, 15)))
    assert_error(project, "decisions.yaml", "Реш-1", "review_date", "возьмите его в кавычки")
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(review_date="2027-13-45"))
    assert_error(project, "decisions.yaml", "Реш-1", "review_date", "2027-13-45", "в кавычках")


def test_decision_references_must_exist(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(option="Вар-9"))
    assert_error(project, "decisions.yaml", "Реш-1", "option", "Вар-9")
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0].update(option="Вар-2", rejected=["Вар-8"])
    )
    assert_error(project, "decisions.yaml", "Реш-1", "rejected", "Вар-8")
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(rejected=[], basis=["У-99"]))
    assert_error(project, "decisions.yaml", "Реш-1", "basis", "несуществующее утверждение У-99")
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0].update(basis=["У-2"], assumptions=["У-98"])
    )
    assert_error(project, "decisions.yaml", "Реш-1", "assumptions", "У-98")


def test_decision_rejected_does_not_contain_chosen(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["rejected"].append("Вар-2"))
    assert_error(project, "decisions.yaml", "Реш-1", "среди отвергнутых")


def test_decision_replacement_is_symmetric(project):
    def replace(d: dict[str, Any]) -> None:
        old = d["decisions"][0]
        new = dict(old, id="Реш-2", replaces="Реш-1", status="действует")
        old.update(status="заменено", replaced_by="Реш-2")
        d["decisions"].append(new)

    edit_yaml(project, "decisions.yaml", replace)
    assert errors_of(project) == []
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][1].pop("replaces"))
    assert_error(project, "decisions.yaml", "Реш-1", "у Реш-2 нет обратной ссылки replaces: Реш-1")
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][1].update(replaces="Реш-1"))
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].pop("replaced_by"))
    assert_error(project, "decisions.yaml", "Реш-1", "статус «заменено» без ссылки на замену")


def test_decision_replacement_has_status_and_target(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(replaced_by="Реш-2"))
    assert_error(project, "decisions.yaml", "Реш-1", "replaced_by задано при статусе «действует»")
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0].update(status="заменено", replaced_by="Реш-9")
    )
    assert_error(project, "decisions.yaml", "Реш-1", "replaced_by", "Реш-9")


def test_decision_replacement_cycle_is_reported(project):
    def cycle(d: dict[str, Any]) -> None:
        a = d["decisions"][0]
        b = dict(a, id="Реш-2")
        a.update(status="заменено", replaced_by="Реш-2", replaces="Реш-2")
        b.update(status="заменено", replaced_by="Реш-1", replaces="Реш-1")
        d["decisions"].append(b)

    edit_yaml(project, "decisions.yaml", cycle)
    assert_error(project, "decisions.yaml", "цикл замены")


def test_decision_status_is_from_the_list(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(status="пересмотрено"))
    assert_error(
        project, "decisions.yaml", "Реш-1", "status", "допустимо: «действует», «заменено» или «отменено»"
    )


def test_decision_number_with_latin_letters_is_explained(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(id="P" + "еш-1"))
    assert_error(project, "decisions.yaml", "латинские буквы", "«P»")


# --- общие правила ----------------------------


@pytest.mark.parametrize("name", NEW_FILES)
def test_unknown_fields_are_rejected(project, name):
    edit_yaml(project, name, lambda d: d.update(лишнее=1))
    assert_error(project, name, "неизвестное поле")


@pytest.mark.parametrize("name", NEW_FILES)
def test_schema_version_is_required(project, name):
    edit_yaml(project, name, lambda d: d.pop("schema_version"))
    assert_error(project, name, "schema_version", "нет обязательного поля")


@pytest.mark.parametrize("name", NEW_FILES)
def test_new_files_follow_encoding_rules(project, name):
    path = project / name
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert_error(project, name, "CRLF")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\r\n", b"\n"))
    assert_error(project, name, "BOM")


@pytest.mark.parametrize("name", NEW_FILES)
def test_new_files_reject_duplicate_keys(project, name):
    path = project / name
    put(path, path.read_text(encoding="utf-8") + "schema_version: 1\n")
    assert_error(project, name, "повторяющийся ключ schema_version")


def test_schemas_validate_the_example(project):
    """Файлы примера разбираются моделями, из которых построены схемы."""
    for name, model in (
        ("plan.yaml", PlanFile),
        ("gaps.yaml", GapsFile),
        ("options.yaml", OptionsFile),
        ("verification.yaml", VerificationFile),
    ):
        assert model.model_validate(load(project, name))
