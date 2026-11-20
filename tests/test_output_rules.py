"""Правила файлов выводов: обходы счётных минимумов, связи между файлами, мелочи.

Каждый тест держит одно правило: без него в коде он падает.
Пример — `tests/fixtures/project_valid/`; ошибки в консоль не печатаются
(консоль Windows бывает в cp1251).
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest

from strateg.models import Branch, Differs, FrameFile
from strateg.project import Project, history_path
from strateg.project_check import OUTPUT_FILES
from test_output_files import load, option, put, synthetic_claim
from test_project_check import FIXTURE, assert_error, edit_yaml, errors_of


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    return dest


def add_synthetic(project: Path) -> None:
    edit_yaml(project, "claims.yaml", lambda d: d["claims"].append(synthetic_claim()))


# --- 23: минимумы считают разные записи ----------------------------


def test_premortem_causes_must_differ(project):
    edit_yaml(project, "verification.yaml", lambda d: d.update(premortem=[dict(d["premortem"][0])] * 3))
    assert_error(project, "verification.yaml", "причины пре-мортема №1 и №2 совпадают", "«claim»")


def test_premortem_cause_with_new_claim_but_same_signal_is_a_copy(project):
    def change(d: dict[str, Any]) -> None:
        d["premortem"][1]["signal"] = d["premortem"][0]["signal"].upper() + "."

    edit_yaml(project, "verification.yaml", change)
    assert_error(project, "verification.yaml", "причины пре-мортема №1 и №2 совпадают", "«signal»")


def test_objections_of_an_option_must_differ(project):
    def change(d: dict[str, Any]) -> None:
        obj = option(d, "Вар-2")["objections"]
        obj[1] = {**obj[0], "hits_assumption": "другое допущение"}
        obj[1]["drops_if"] = obj[0]["drops_if"].upper() + "."

    edit_yaml(project, "options.yaml", change)
    assert_error(project, "options.yaml", "запись Вар-2 (№2)", "возражения №1 и №2 совпадают", "drops_if")


def test_option_titles_must_differ(project):
    def change(d: dict[str, Any]) -> None:
        twin = copy.deepcopy(option(d, "Вар-2"))
        twin.update(id="Вар-5", title=twin["title"].upper())
        twin["differs"]["from"] = "Вар-1"
        d["options"].append(twin)

    edit_yaml(project, "options.yaml", change)
    assert_error(project, "options.yaml", "названия вариантов Вар-2 и Вар-5 совпадают")


# --- 24: причина пре-мортема ведёт в защитный показатель ----------------------------


def test_premortem_row_must_be_a_guard_row(project):
    assert load(project, "verification.yaml")["rows"][0]["kind"] == "основной"
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][0].update(row="ПП-1"))
    assert_error(
        project, "verification.yaml", "premortem, запись №1", "строка ПП-1 основная", "защитный показатель"
    )
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][0].update(row="ПП-2"))
    assert errors_of(project) == []


# --- 25: условие решения связано со строкой плана проверки ----------------------------


def test_condition_row_must_exist(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(row="ПП-9"))
    assert_error(
        project, "decisions.yaml", "запись Реш-1 (№1)", "conditions[0].row", "ПП-9", "verification.yaml"
    )
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(row="ПП-"))
    assert_error(project, "decisions.yaml", "conditions[0].row", "номер строки плана проверки вида ПП-1")


def test_condition_row_needs_verification_file(project):
    (project / "verification.yaml").unlink()
    assert_error(project, "decisions.yaml", "Реш-1", "conditions[0].row", "файла verification.yaml нет")


def test_condition_threshold_must_equal_row_threshold(project):
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(threshold="ниже 25 %")
    )
    assert_error(project, "decisions.yaml", "conditions[0].row", "«ниже 25 %»", "не совпадает", "ПП-5")
    edit_yaml(
        project,
        "decisions.yaml",
        lambda d: d["decisions"][0]["conditions"][0].update(threshold="Ниже  20 %."),
    )
    assert errors_of(project) == []


def test_condition_row_stays_optional(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].pop("row"))
    assert errors_of(project) == []


# --- 26: синтетика не опора ----------------------------


def test_recommendation_not_on_synthetic(project):
    add_synthetic(project)
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-2", "У-7"]))
    assert_error(project, "options.yaml", "recommendation", "claims", "У-7 — синтетика")


def test_recommendation_needs_a_fact(project):
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-4"]))
    assert_error(project, "options.yaml", "recommendation", "нет ни одного факта")
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-3"]))
    assert_error(project, "options.yaml", "recommendation", "нет ни одного факта")
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-2", "У-3"]))
    assert errors_of(project) == []


def test_metric_coverage_is_fact_or_forecast(project):
    add_synthetic(project)
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(claims=["У-7"]))
    assert_error(project, "gaps.yaml", "metric_coverage, запись №1", "claims", "У-7 — синтетика")
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(claims=["У-3"]))
    assert_error(
        project, "gaps.yaml", "metric_coverage, запись №1", "У-3 имеет тип «гипотеза»", "«факт» или «прогноз»"
    )
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][0].update(claims=["У-2", "У-4"]))
    assert errors_of(project) == []


def test_closed_gap_not_on_synthetic(project):
    add_synthetic(project)
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт", closed_by=["У-7"]))
    assert_error(project, "gaps.yaml", "запись П-1 (№1)", "closed_by", "У-7 — синтетика")


def test_decision_basis_not_on_synthetic(project):
    add_synthetic(project)
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(basis=["У-7"]))
    assert_error(project, "decisions.yaml", "запись Реш-1 (№1)", "basis", "У-7 — синтетика")


# --- 27: владелец — роль ----------------------------


@pytest.mark.parametrize(
    "owner",
    [
        "вся команда",
        "команда продукта",
        "все, команда",
        "команда/все",
        "n/a",
        "N/A",
        "-",
        "?",
        "не назначен",
        "Не назначен.",
        "T B D",
        "\u0422\u0412D",  # русские Т и В, латинская D
        "tb\u200bd",  # нулевой пробел внутри
        "к\u006fманда",  # латинская o в русском слове
        "tba",
    ],
)
def test_owner_variants_are_not_roles(project, owner):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(owner=owner))
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "owner", "не роль")


@pytest.mark.parametrize(
    "owner", ["руководитель направления", "аналитик продукта", "руководитель команды разработки"]
)
def test_real_roles_stay_valid(project, owner):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(owner=owner))
    assert errors_of(project) == []


# --- 28: сравнение действий веток ----------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Ещё раз", "еще раз"),
        ("продавать-отдельно", "продавать отдельно"),
        ("запустить «пилот»", "запустить пилот"),
        ("продавать отдельно", "продавать \u200bотдельно"),
        ("продавать отдельно", "Продавать отдельно!"),
        ("продавать\u2013отдельно", "продавать-отдельно"),
    ],
)
def test_branch_actions_are_compared_like_quotes(project, first, second):
    def change(d: dict[str, Any]) -> None:
        d["questions"][0]["branches"][0]["then"] = first
        d["questions"][0]["branches"][1]["then"] = second

    edit_yaml(project, "plan.yaml", change)
    assert_error(project, "plan.yaml", "запись ВП-1 (№1)", "одинаковое действие")


def test_different_actions_stay_valid(project):
    edit_yaml(
        project,
        "plan.yaml",
        lambda d: d["questions"][0]["branches"][1].update(then="продавать вместе с тарифом"),
    )
    assert errors_of(project) == []


# --- 29: «почти номера» в свободных полях ----------------------------


@pytest.mark.parametrize(
    "value", ["\u0412P-9", "BP-9", "\u0412\u041f-09", "вп-9", "ВП-2, ВП-9", "ВП-2 и ВП-3", "ВП - 2"]
)
def test_plan_question_that_looks_like_a_number_is_explained(project, value):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(plan_question=value))
    assert_error(project, "claims.yaml", "запись У-3 (№3)", "plan_question", "не подходит", "ВП-1")


def test_plan_question_latin_letter_is_named(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(plan_question="\u0412P-9"))
    assert_error(project, "claims.yaml", "У-3", "латинские буквы", "«P»")


@pytest.mark.parametrize("value", ["P-1", "П-01", "п-1", "p-2", "П - 3"])
def test_baseline_that_looks_like_a_number_is_explained(project, value):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline=value))
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "baseline", "похоже на номер пробела")


@pytest.mark.parametrize(
    "value",
    [
        "2 % в месяц",
        "данных нет, измеряется впервые",
        "Пробел в данных не закрыт",
        "P-50 перцентиль времени отклика",
        "П-1 за прошлый месяц",
    ],
)
def test_baseline_text_stays_valid(project, value):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline=value))
    assert errors_of(project) == []


# --- 30: проверка history/ ----------------------------


def test_frame_version_has_an_upper_limit(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=3_000_000))
    errors = assert_error(project, "frame.yaml", "version", "вне допустимых границ")
    assert len(errors) == 1


def test_missing_versions_are_one_error(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=1000))
    errors = errors_of(project)
    assert len(errors) == 1
    assert "версия 1000" in errors[0] and "2…999" in errors[0]


def test_missing_versions_are_listed_as_ranges(project):
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=8))
    errors = errors_of(project)
    assert len(errors) == 1 and "2…7" in errors[0]


def test_version_above_current_is_an_error(project):
    shutil.copy(project / "history" / "frame.v1.yaml", project / "history" / "frame.v5.yaml")
    assert_error(project, "history/frame.v5.yaml", "версия 5 не прежняя")
    shutil.copy(project / "history" / "frame.v1.yaml", project / "history" / "frame.v2.yaml")
    assert_error(project, "history/frame.v2.yaml", "версия 2 не прежняя")


def test_previous_version_must_be_a_readable_frame(project):
    (project / "history" / "frame.v1.yaml").write_bytes(b"\xff\xfe")
    assert_error(project, "history/frame.v1.yaml", "не в кодировке UTF-8")
    put(project / "history" / "frame.v1.yaml", "schema_version: 1\nversion: 1\n")
    assert_error(project, "history/frame.v1.yaml", "decision", "нет обязательного поля")


def test_previous_version_number_matches_its_name(project):
    text = (project / "history" / "frame.v1.yaml").read_text(encoding="utf-8")
    put(project / "history" / "frame.v1.yaml", text.replace("\nversion: 1\n", "\nversion: 3\n"))
    assert_error(project, "history/frame.v1.yaml", "версия рамки 3", "в имени 1")


def test_service_files_are_not_versions(project):
    for name in (".gitkeep", ".DS_Store", "Thumbs.db"):
        put(project / "history" / name, "")
    assert errors_of(project) == []


# --- 31: history_path бросает один вид ошибки ----------------------------


@pytest.mark.parametrize("path", ["../x.yaml", "/abs.yaml", "", "outputs/", "C:/x.yaml", "a//b.md"])
def test_history_path_rejects_a_bad_path_with_value_error_in_russian(path):
    with pytest.raises(ValueError) as caught:
        history_path(path, 1)
    assert type(caught.value) is ValueError
    text = str(caught.value)
    assert "не подходит" in text and "validation error" not in text


def test_history_path_rejects_a_bad_version_in_russian():
    with pytest.raises(ValueError, match="номер версии"):
        history_path("frame.yaml", 0)


# --- 32: дата строго из цифр ASCII ----------------------------


@pytest.mark.parametrize(
    "value", ["2026-11-06\n", "\uff12\uff10\uff12\uff16-11-06", " 2026-11-06", "2026-11-06 "]
)
def test_date_is_ascii_digits_only(project, value):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0]["searched"][0].update(date=value))
    assert_error(project, "gaps.yaml", "П-1", "searched[0].date", "в кавычках")


# --- 33: from_change_mind сверяется с рекомендацией ----------------------------


def test_from_change_mind_threshold_must_match_the_recommendation(project):
    edit_yaml(
        project, "verification.yaml", lambda d: d["rows"][4]["change_mind"].update(threshold="ниже 5 %")
    )
    assert_error(
        project, "verification.yaml", "запись ПП-5 (№5)", "from_change_mind", "не совпадает", "ниже 20 %"
    )


def test_other_row_cannot_claim_the_recommendation_threshold(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(from_change_mind=1))
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "from_change_mind", "не совпадает")


def test_equal_threshold_after_normalization_is_fine(project):
    edit_yaml(
        project, "verification.yaml", lambda d: d["rows"][4]["change_mind"].update(threshold="Ниже  20 %.")
    )
    assert errors_of(project) == []


# --- 34: формулировки ----------------------------


def test_case_in_a_number_hint(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(affects_options=["B" + "ар-1"]))
    assert_error(project, "gaps.yaml", "П-1", "нужен номер варианта вида Вар-1")


def test_allowed_values_with_a_comma_are_unambiguous(project):
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(origin="передумаю")
    )
    assert_error(project, "decisions.yaml", "допустимо: «передумаю, если» или «пре-мортем»")


def test_file_and_field_of_a_frame_source_are_separate(project):
    edit_yaml(project, "frame.yaml", lambda d: d["data"].update(source="И-9"))
    assert_error(project, "frame.yaml, поле data.source: ссылка на несуществующий источник И-9")


def test_premortem_is_exactly_three(project):
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"].pop())
    errors = assert_error(project, "verification.yaml", "нужно ровно 3")
    assert not any("не меньше 3" in e or "не больше 3" in e for e in errors)


# --- 35: тесты на ослабления ----------------------------


def test_plan_numbers_are_not_repeated(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][1].update(id="ВП-1"))
    assert_error(project, "plan.yaml", "номер ВП-1 встречается больше одного раза")


def test_gap_numbers_are_not_repeated(project):
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][1].update(id="П-1"))
    assert_error(project, "gaps.yaml", "номер П-1 встречается больше одного раза")


def test_promise_numbers_are_not_repeated(project):
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][1].update(id="О-1"))
    assert_error(project, "promises.yaml", "номер О-1 встречается больше одного раза")


def test_decision_numbers_are_not_repeated(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"].append(copy.deepcopy(d["decisions"][0])))
    assert_error(project, "decisions.yaml", "номер Реш-1 встречается больше одного раза")


def test_replaced_decision_must_have_status_replaced(project):
    def change(d: dict[str, Any]) -> None:
        d["decisions"].append({**copy.deepcopy(d["decisions"][0]), "id": "Реш-2", "replaces": "Реш-1"})

    edit_yaml(project, "decisions.yaml", change)
    assert_error(project, "decisions.yaml", "Реш-2", "заменяет Реш-1, но у него статус «действует»")


def test_decision_cannot_replace_itself(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(replaces="Реш-1"))
    assert_error(project, "decisions.yaml", "Реш-1", "заменяет само себя")


def test_hashes_of_new_files_and_history_go_to_state(project):
    loaded = Project.load(project)
    loaded.state.files = {}
    loaded.save_state()
    files = loaded.state.files
    for name in OUTPUT_FILES:
        assert name in files, name
    assert history_path("frame.yaml", 1) in files


# --- 36: gap остаётся у закрытого вопроса плана ----------------------------


def test_closed_plan_question_keeps_its_gap(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][2].update(status="закрыт"))
    assert errors_of(project) == []
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][2].update(status="открыт"))
    assert_error(project, "plan.yaml", "ВП-3", "gap задано при статусе «открыт»")


# --- 37: хеши history/ ----------------------------


def test_history_files_are_hashed(project):
    shutil.copy(project / "history" / "frame.v1.yaml", project / "history" / "frame.v2.yaml")
    put(
        project / "history" / "frame.v2.yaml",
        (project / "history" / "frame.v1.yaml")
        .read_text(encoding="utf-8")
        .replace("\nversion: 1\n", "\nversion: 2\n"),
    )
    edit_yaml(project, "frame.yaml", lambda d: d.update(version=3))
    loaded = Project.load(project)
    loaded.save_state()
    assert {"history/frame.v1.yaml", "history/frame.v2.yaml"} <= set(loaded.state.files)


# --- 38: описание укладывается с запасом ----------------------------


def test_data_model_doc_keeps_room_and_has_no_wall_of_text():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "data-model.md").read_text(encoding="utf-8")
    assert len(doc.encode("utf-8")) <= 17000  # тот же предел, что в test_data_model_doc.py
    assert max(len(line.encode("utf-8")) for line in doc.splitlines()) <= 1200


# --- 39: допущения решения ----------------------------


def test_decision_assumptions_are_assumptions_or_hypotheses(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(assumptions=["У-2"]))
    assert_error(
        project,
        "decisions.yaml",
        "Реш-1",
        "assumptions",
        "У-2 имеет тип «факт»",
        "«допущение» или «гипотеза»",
    )
    for ref in ("У-1", "У-3"):
        edit_yaml(project, "decisions.yaml", lambda d, ref=ref: d["decisions"][0].update(assumptions=[ref]))
        assert errors_of(project) == []


# --- 40: заглушки ----------------------------


@pytest.mark.parametrize("stub", ["-", "нет", "—", "1234567890", "...........", " \u00a0 "])
def test_why_empty_is_words(project, stub):
    edit_yaml(
        project, "options.yaml", lambda d: option(d, "Вар-1")["comparison"]["effect"].update(why_empty=stub)
    )
    errors = errors_of(project)
    assert any("comparison.effect.why_empty" in e for e in errors), errors


def test_short_but_honest_reason_is_fine(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: option(d, "Вар-1")["comparison"]["effect"].update(why_empty="нет данных"),
    )
    assert errors_of(project) == []


def test_decision_threshold_has_a_number(project):
    edit_yaml(
        project,
        "decisions.yaml",
        lambda d: d["decisions"][0]["conditions"][0].update(threshold="по ситуации"),
    )
    assert_error(project, "decisions.yaml", "запись Реш-1 (№1)", "conditions[0].threshold", "нет числа")


def test_row_threshold_has_a_number(project):
    edit_yaml(
        project, "verification.yaml", lambda d: d["rows"][0]["change_mind"].update(threshold="заметно ниже")
    )
    assert_error(project, "verification.yaml", "запись ПП-1 (№1)", "change_mind.threshold", "нет числа")


def test_recommendation_threshold_has_a_number(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: d["recommendation"]["change_mind"][0].update(threshold="заметно ниже"),
    )
    assert_error(project, "options.yaml", "recommendation.change_mind[0].threshold", "нет числа")


def test_premortem_threshold_has_a_number(project):
    edit_yaml(project, "verification.yaml", lambda d: d["premortem"][0].update(threshold="сильно выше"))
    assert_error(project, "verification.yaml", "premortem, запись №1", "threshold", "нет числа")


# --- 42: модели собираются по имени поля ----------------------------


def test_models_are_built_by_field_name_and_by_file_key():
    by_name = Branch(if_="цена ниже", then="продавать отдельно")
    assert by_name == Branch.model_validate({"if": "цена ниже", "then": "продавать отдельно"})
    assert Differs(axis="аудитория", from_="Вар-1", how="другая группа").from_ == "Вар-1"


def test_file_key_with_underscore_is_still_an_error(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0]["branches"][0].update(if_="x"))
    assert_error(project, "plan.yaml", "ВП-1", "branches[0].if_", "неизвестное поле")


def test_frame_file_still_builds_from_the_example():
    assert FrameFile.model_validate(load(FIXTURE, "frame.yaml")).version == 2


def test_doc_says_what_decision_assumptions_hold():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "data-model.md").read_text(encoding="utf-8")
    assert "допущение или гипотеза у `decisions[].assumptions`" in doc


# --- правки по второму раунду ----------------------------


def test_followup_1_file_keys_with_underscore_instead_of_if_and_from_are_rejected(project):
    """Ключ `if_` вместо `if` и `from_` вместо `from` в файле — ошибка; пары рядом нет."""

    def plan(d: dict[str, Any]) -> None:
        branch = d["questions"][0]["branches"][0]
        branch["if_"] = branch.pop("if")

    def options(d: dict[str, Any]) -> None:
        differs = option(d, "Вар-2")["differs"]
        differs["from_"] = differs.pop("from")

    edit_yaml(project, "plan.yaml", plan)
    errors = assert_error(project, "plan.yaml", "ВП-1", "branches[0].if_", "неизвестное поле")
    assert any("branches[0].if" in e and "нет обязательного поля" in e for e in errors)
    edit_yaml(project, "options.yaml", options)
    errors = assert_error(project, "options.yaml", "Вар-2", "differs.from_", "неизвестное поле")
    assert any("differs.from" in e and "нет обязательного поля" in e for e in errors)


@pytest.mark.parametrize("value", ["P-50 перцентиль времени отклика", "П-1 за прошлый месяц"])
def test_followup_2_baseline_with_words_after_a_number_is_text(project, value):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline=value))
    assert errors_of(project) == []


def test_followup_3_owner_error_names_the_word_and_gives_an_example(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(owner="команда продукта"))
    assert_error(
        project, "verification.yaml", "owner", "слово «команда» не допускается", "руководитель продукта"
    )
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(owner="?"))
    assert_error(project, "verification.yaml", "owner", "в значении нет слов", "руководитель продукта")


@pytest.mark.parametrize("second", ["ниже 20%", "Ниже 20 %.", "ниже  20  %"])
def test_followup_4_thresholds_differ_only_in_spaces_around_signs(project, second):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][4]["change_mind"].update(threshold=second))
    assert errors_of(project) == []
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0]["conditions"][0].update(threshold=second)
    )
    assert errors_of(project) == []


def test_followup_4_different_thresholds_stay_different(project):
    edit_yaml(
        project, "verification.yaml", lambda d: d["rows"][4]["change_mind"].update(threshold="ниже 2 0%")
    )
    assert_error(project, "verification.yaml", "from_change_mind", "не совпадает")


def test_followup_5_objection_basis_is_not_synthetic_and_is_fact_or_hypothesis(project):
    add_synthetic(project)
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][0].update(basis="У-7"))
    assert_error(project, "options.yaml", "Вар-2", "objections[0].basis", "У-7 — синтетика")
    edit_yaml(project, "options.yaml", lambda d: option(d, "Вар-2")["objections"][0].update(basis="У-4"))
    assert_error(
        project,
        "options.yaml",
        "Вар-2",
        "objections[0].basis",
        "У-4 имеет тип «прогноз»",
        "«факт» или «гипотеза»",
    )
    for ref in ("У-2", "У-3"):
        edit_yaml(
            project, "options.yaml", lambda d, ref=ref: option(d, "Вар-2")["objections"][0].update(basis=ref)
        )
        assert errors_of(project) == []


def test_followup_6_threshold_without_a_number_suggests_zero_or_one(project):
    edit_yaml(
        project, "verification.yaml", lambda d: d["rows"][0]["change_mind"].update(threshold="в любом случае")
    )
    assert_error(project, "verification.yaml", "change_mind.threshold", "нет числа", "порог 0 или 1")


def test_followup_7_text_key_keeps_percent_and_decimal_separator():
    from strateg.models import text_key

    assert text_key("доля ниже 20%") != text_key("доля ниже 20")
    assert text_key("доля ниже 20%") == text_key("Доля ниже 20 %.")
    assert text_key("рост 0,5") != text_key("рост 05")
    assert text_key("рост 0.5") != text_key("рост 05")
    assert text_key("рост, 5") == text_key("рост 5")


def test_followup_7_branches_that_differ_only_in_percent_are_not_a_copy(project):
    def change(d: dict[str, Any]) -> None:
        d["questions"][0]["branches"][0]["then"] = "поднять цену на 20%"
        d["questions"][0]["branches"][1]["then"] = "поднять цену на 20"

    edit_yaml(project, "plan.yaml", change)
    assert errors_of(project) == []


def test_followup_7_objections_that_differ_only_in_percent_are_not_a_copy(project):
    def change(d: dict[str, Any]) -> None:
        obj = option(d, "Вар-2")["objections"]
        obj[0]["drops_if"] = "продлений меньше 20%"
        obj[1]["drops_if"] = "продлений меньше 20"

    edit_yaml(project, "options.yaml", change)
    assert errors_of(project) == []
