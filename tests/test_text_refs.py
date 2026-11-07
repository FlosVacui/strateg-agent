"""Номера `П-n`, `У-n` и `Вар-n` в свободном тексте файлов выхода сверяются с реестрами, как ссылки.

Раньше пояснение «см. П-9» в `why_empty` проходило проверку молча, хотя пробела П-9 нет.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from helpers import FIXTURE, fixture_outputs, tree_hashes
from strateg.project_check import check_project
from test_output_files import option
from test_project_check import assert_error, edit_yaml, errors_of


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    return dest


def set_why_empty(text: str):
    def change(d: dict[str, Any]) -> None:
        option(d, "Вар-1")["comparison"]["cost"] = {"why_empty": text}

    return change


def test_why_empty_with_unknown_gap_number_is_an_error(project):
    edit_yaml(project, "options.yaml", set_why_empty("данных нет, закрыть можно только после П-9"))
    assert_error(
        project,
        "options.yaml, запись Вар-1",
        "comparison.cost.why_empty",
        "в тексте названо П-9",
        "такого номера нет в gaps.yaml",
    )


def test_why_empty_with_known_gap_number_is_fine(project):
    edit_yaml(project, "options.yaml", set_why_empty("данных нет, закрыть можно только после П-2"))
    assert errors_of(project) == []


@pytest.mark.parametrize(
    ("text", "needles"),
    [
        ("база появится после проверки У-99", ("в тексте названо У-99", "такого утверждения нет")),
        (
            "как в варианте Вар-9, только дешевле",
            ("в тексте названо Вар-9", "такого номера нет в options.yaml"),
        ),
    ],
)
def test_claim_and_option_numbers_in_text_are_checked(project, text, needles):
    edit_yaml(project, "options.yaml", set_why_empty(text))
    assert_error(project, "comparison.cost.why_empty", *needles)


def test_known_claim_and_option_numbers_in_text_are_fine(project):
    edit_yaml(
        project, "options.yaml", set_why_empty("основание — У-2, как в варианте Вар-2, закрыть можно П-1")
    )
    assert errors_of(project) == []


@pytest.mark.parametrize(
    "text",
    [
        "базы нет, диапазон по выручке тарифов не оценить",
        "строка плана ПП-1 и вопрос ВП-2 не меняют ответ",  # «П-1» внутри «ПП-1» и «ВП-2» — не номер пробела
        "П-образный график и У-образная кривая не номера",
        "версия СП-9 и КП-9, а также ОУ-5 — другие обозначения",
        "Вар- без номера, П- и У- тоже без номера",
        "П-0 и П-01 — не номера",
    ],
)
def test_text_without_numbers_gives_no_false_positives(project, text):
    edit_yaml(project, "options.yaml", set_why_empty(text))
    assert errors_of(project) == []


def test_a_pure_reference_is_checked_by_its_own_field_only(project):
    """Поле, в котором стоит один номер, — ссылка: ошибка одна, а не две."""
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline="П-9"))
    errors = [e for e in errors_of(project) if "П-9" in e]
    assert len(errors) == 1 and "ссылка на П-9" in errors[0]


def test_baseline_text_with_unknown_gap_number_is_an_error(project):
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(baseline="П-9 (см.)"))
    assert_error(project, "verification.yaml", "запись ПП-1", "baseline", "в тексте названо П-9")


def test_every_output_file_is_scanned(project):
    edit_yaml(
        project, "decisions.yaml", lambda d: d["decisions"][0].update(expected="решение зависит от П-9")
    )
    assert_error(project, "decisions.yaml", "запись Реш-1", "expected", "в тексте названо П-9")
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][1].update(conditions="нужен У-77"))
    assert_error(project, "promises.yaml", "запись О-2", "conditions", "в тексте названо У-77")
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(how_to_close="сначала закрыть П-8"))
    assert_error(project, "gaps.yaml", "запись П-1", "how_to_close", "в тексте названо П-8")
    edit_yaml(project, "frame.yaml", lambda d: d["decision"].update(value="решить после П-6"))
    assert_error(project, "frame.yaml", "поле decision.value", "в тексте названо П-6")


def test_number_in_text_without_the_registry_file(project):
    (project / "gaps.yaml").unlink()
    errors = errors_of(project)
    assert any("но файла gaps.yaml нет" in e for e in errors)


def test_claims_registry_texts_are_not_scanned(project):
    """Тексты утверждений — цитаты и пересказ источников, а не файлы выхода: «П-5» в них не ссылка."""
    edit_yaml(project, "claims.yaml", lambda d: d["claims"][2].update(text="Подключают защиту по тарифу П-5"))
    assert errors_of(project) == []


def test_write_refuses_text_with_unknown_number_and_changes_nothing(cli, home):
    """Через CLI: запись с пояснением «закроем после П-9» отклоняется, папка остаётся прежней."""
    shutil.copytree(FIXTURE, home / "example")
    assert check_project(home / "example") == []
    options = fixture_outputs()["options"]
    options["options"][0]["comparison"]["cost"] = {"why_empty": "закроем после П-9"}
    before = tree_hashes(home / "example")
    stdin = json.dumps({"options": options}, ensure_ascii=False)
    code, _, err = cli("write", "--file", "-", "--project", "example", stdin=stdin)
    assert code == 1 and "П-9" in err and "такого номера нет в gaps.yaml" in err
    assert tree_hashes(home / "example") == before
