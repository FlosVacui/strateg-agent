"""Рекомендация не держится на заявлении компании о себе (`self_reported: true`).

`write` отказывает, если среди оснований рекомендации нет факта из источника не «о себе». Пояснение —
по-русски и с номерами утверждений. Обещание о возможности своего продукта подтверждает его документация:
у обещаний этой проверки нет, обещание результата оценивает критик.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from helpers import FIXTURE, fixture_outputs, tree_hashes
from strateg.project_check import check_project
from test_project_check import assert_error, claim, edit_yaml, errors_of


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    return dest


def make_self_reported(d: dict[str, Any]) -> None:
    """У-2 держится на двух цитатах: страница компании и отчёт исследователей. Оставляем одну, «о себе»."""
    quotes = claim(d, "У-2")["quotes"]
    del quotes[1:]
    assert quotes[0]["self_reported"] is True


def test_example_stands_on_an_independent_fact(project):
    assert errors_of(project) == []


def test_recommendation_only_on_self_reported_fact_is_rejected(project):
    edit_yaml(project, "claims.yaml", make_self_reported)
    assert_error(
        project, "options.yaml, recommendation", "claims", "только на заявлении компании о себе", "У-2"
    )


def test_confirmed_promise_may_stand_on_the_product_documentation(project):
    edit_yaml(project, "claims.yaml", make_self_reported)
    errors = errors_of(project)
    assert not any("promises.yaml" in e for e in errors)  # возможность описана страницей самой компании
    assert any("options.yaml, recommendation" in e for e in errors)  # а рекомендация отклоняется


def test_partially_confirmed_promise_may_stand_on_self_reported_fact(project):
    edit_yaml(project, "claims.yaml", make_self_reported)
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0].update(status="подтверждено частично"))
    errors = errors_of(project)
    assert not any("promises.yaml" in e for e in errors)  # а рекомендация по-прежнему отклоняется
    assert any("options.yaml, recommendation" in e for e in errors)


def test_one_independent_fact_among_the_grounds_is_enough(project):
    edit_yaml(project, "claims.yaml", make_self_reported)

    def independent_fact(d: dict[str, Any]) -> None:
        other = json.loads(json.dumps(claim(d, "У-2")))
        other.update(id="У-7", replaces=None, text="Подписка стоит 199 рублей в месяц по данным обзора")
        other["quotes"] = [json.loads(json.dumps(claim(d, "У-4")["quotes"][0]))]
        other["quotes"][0].update(text="в течение года вырастет с 12 до 15–17 процентов")
        d["claims"].append(other)

    edit_yaml(project, "claims.yaml", independent_fact)
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-2", "У-7"]))
    errors = errors_of(project)
    assert not any("только на заявлении компании о себе" in e for e in errors if "options.yaml" in e)


def test_fact_with_a_self_reported_quote_and_an_independent_quote_counts_as_independent(project):
    # в примере У-2 именно такая: цитата страницы компании и цитата отчёта
    flags = [q["self_reported"] for q in claim(_load(project), "У-2")["quotes"]]
    assert sorted(flags) == [False, True]
    assert errors_of(project) == []


def _load(project: Path) -> dict[str, Any]:
    return yaml.safe_load((project / "claims.yaml").read_text(encoding="utf-8"))


def test_message_is_in_russian_and_names_the_claims(project):
    edit_yaml(project, "claims.yaml", make_self_reported)
    text = "\n".join(e for e in errors_of(project) if "только на заявлении компании о себе" in e)
    assert "У-2" in text and "self_reported: true" in text and "независимого источника" in text


def test_write_refuses_and_leaves_the_folder_untouched(cli, home):
    shutil.copytree(FIXTURE, home / "example")
    assert check_project(home / "example") == []
    edit_yaml(home / "example", "claims.yaml", make_self_reported)
    # ошибка в записываемом файле мешает записи, даже если появилась до неё
    data = fixture_outputs()
    data["options"]["recommendation"]["why"] += " (уточнено)"
    before = tree_hashes(home / "example")
    stdin = json.dumps({"options": data["options"]}, ensure_ascii=False)
    code, _, err = cli("write", "--file", "-", "--project", "example", stdin=stdin)
    assert code == 1
    assert "только на заявлении компании о себе" in err and "У-2" in err
    assert tree_hashes(home / "example") == before
