"""Синтетика не доходит до оснований ни напрямую, ни через утверждение-посредник, ни пересказом (ДВ-1).

Проект — образец с одним синтетическим утверждением У-7 и источником И-3 вида «синтетика».
"""

# ruff: noqa: F811
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import claims_of
from strateg import project_check
from strateg.models import ClaimsFile, SourcesFile
from strateg.project_check import check_project, synthetic_claims
from test_output_files import synthetic_claim
from test_project_check import FIXTURE, assert_error, edit_yaml, errors_of
from test_synth import add_claim, claim_in, session, synth_source  # noqa: F401


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    edit_yaml(dest, "claims.yaml", lambda d: d["claims"].append(synthetic_claim()))
    synth_source(dest)
    return dest


def other(cid: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": cid,
        "origin": "агент",
        "step": "2б",
        "attempt": 1,
        "subject": "цена",
        "status": "действует",
        "status_history": [{"status": "действует", "at": "2026-11-06T10:50:00+03:00", "by": "агент"}],
    }
    base.update(kw)
    return base


def hypothesis(cid: str, basis: list[str], text: str = "Пользователи жалуются на цену") -> dict[str, Any]:
    return other(cid, text=text, type="гипотеза", basis=basis, check_method="спросить людей")


def assumption(cid: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("taken_from", "ответы пользователя")
    return other(cid, text="Пользователи жалуются на цену", type="допущение", **kw)


def put_claims(root: Path, *items: dict[str, Any]) -> None:
    edit_yaml(root, "claims.yaml", lambda d: d["claims"].extend(items))


def put_in_grounds(root: Path, cid: str) -> None:
    """Утверждение стоит в основаниях рекомендации и решения."""
    edit_yaml(root, "options.yaml", lambda d: d["recommendation"].update(claims=["У-2", cid]))
    edit_yaml(root, "decisions.yaml", lambda d: d["decisions"][0].update(basis=["У-2", cid]))


# --- допущение на синтетике --------------------------------------------------------------------------


def test_clean_project_with_a_synthetic_claim_and_source_has_no_errors(project):
    assert errors_of(project) == []


def test_assumption_with_a_synthetic_basis_is_an_error_at_the_claim(project):
    put_claims(project, assumption("У-8", basis=["У-7"]))
    assert_error(project, "claims.yaml", "У-8", "поле basis", "У-7 — синтетика")


def test_assumption_on_a_synthetic_basis_does_not_reach_gaps_and_decisions(project):
    put_claims(project, assumption("У-8", basis=["У-7"]))
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(assumption="У-8"))
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0].update(assumptions=["У-8"]))
    put_in_grounds(project, "У-8")
    errors = errors_of(project)
    assert any("У-8" in e and "опирается только на синтетику" in e and "options.yaml" in e for e in errors)
    assert any("У-8" in e and "опирается только на синтетику" in e and "decisions.yaml" in e for e in errors)
    assert_error(project, "claims.yaml", "У-8", "поле basis", "У-7 — синтетика")


def test_assumption_retelling_a_session_in_taken_from_is_an_error(project):
    put_claims(project, assumption("У-8", taken_from="синтетическая сессия И-3: модельные респонденты"))
    assert_error(project, "claims.yaml", "У-8", "поле taken_from", "И-3", "синтетика")


def test_assumption_retelling_a_session_does_not_close_a_gap_or_stand_in_the_recommendation(project):
    put_claims(project, assumption("У-8", taken_from="синтетическая сессия И-3: модельные респонденты"))
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-2", "У-8"]))
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт", closed_by=["У-8"]))
    errors = errors_of(project)
    assert any("options.yaml" in e and "recommendation" in e and "У-8 опирается" in e for e in errors)
    assert any("gaps.yaml" in e and "closed_by" in e and "У-8 опирается" in e for e in errors)


def test_a_session_named_in_the_text_of_a_non_hypothesis_is_an_error(project):
    put_claims(project, assumption("У-8", taken_from="разговор с клиентом"))
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: d["claims"][-1].update(text="Все респонденты сессии И-3 жалуются на цену"),
    )
    assert_error(project, "claims.yaml", "У-8", "поле text", "И-3")


def test_a_hypothesis_may_name_the_session_in_its_text(project):
    put_claims(project, hypothesis("У-8", ["У-7"], text="По сессии И-3 цена важнее скорости"))
    assert errors_of(project) == []


# --- гипотеза и цепочка basis ------------------------------------------------------------------------


def test_hypothesis_only_on_synthetic_is_not_a_ground_of_the_recommendation_or_the_decision(project):
    put_claims(project, hypothesis("У-8", ["У-7"]))
    put_in_grounds(project, "У-8")
    errors = errors_of(project)
    assert any(
        "options.yaml" in e and "recommendation" in e and "У-8 опирается только на синтетику" in e
        for e in errors
    )
    assert any(
        "decisions.yaml" in e and "basis" in e and "У-8 опирается только на синтетику" in e for e in errors
    )


def test_the_chain_of_basis_counts_through_several_claims(project):
    put_claims(project, hypothesis("У-8", ["У-7"]), hypothesis("У-9", ["У-8"], text="Цена главное"))
    put_in_grounds(project, "У-9")
    assert any("У-9 опирается только на синтетику" in e for e in errors_of(project))


def test_a_hypothesis_with_a_real_basis_among_the_synthetic_one_stays_a_ground(project):
    put_claims(project, hypothesis("У-8", ["У-7", "У-4"]))
    put_in_grounds(project, "У-8")
    assert errors_of(project) == []


def test_a_closed_gap_and_metric_coverage_do_not_stand_on_a_derived_hypothesis(project):
    put_claims(project, hypothesis("У-8", ["У-7"]))
    edit_yaml(project, "gaps.yaml", lambda d: d["gaps"][0].update(status="закрыт", closed_by=["У-8"]))
    assert_error(project, "gaps.yaml", "closed_by", "У-8 опирается только на синтетику")

    def cover(d: dict[str, Any]) -> None:
        d["metric_coverage"][0]["claims"] = ["У-8"]

    edit_yaml(project, "gaps.yaml", cover)
    assert_error(project, "gaps.yaml", "metric_coverage", "claims", "У-8 опирается только на синтетику")


def test_a_fact_or_forecast_on_a_derived_hypothesis_is_refused(project):
    put_claims(project, hypothesis("У-8", ["У-7"]))
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: d["claims"].append(
            other(
                "У-9",
                text="Цена вырастет",
                type="прогноз",
                basis=["У-8"],
                forecast_range={"low": 1, "high": 2, "unit": "%"},
            )
        ),
    )
    assert_error(project, "claims.yaml", "У-9", "поле basis", "У-8 опирается только на синтетику")


def test_synthetic_claims_map_marks_direct_and_derived_and_skips_real_support(project):
    put_claims(project, hypothesis("У-8", ["У-7"]), hypothesis("У-9", ["У-7", "У-4"]))
    import yaml

    claims = ClaimsFile.model_validate(yaml.safe_load((project / "claims.yaml").read_text(encoding="utf-8")))
    sources = SourcesFile.model_validate(
        yaml.safe_load((project / "sources.yaml").read_text(encoding="utf-8"))
    )
    assert synthetic_claims(claims.claims, sources) == {"У-7": "direct", "У-8": "derived"}


# --- текстовые поля ----------------------------------------------------------------------------------


def test_the_session_named_in_words_in_why_is_an_error(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: d["recommendation"].update(
            why=d["recommendation"]["why"] + " Так сказали все респонденты сессии И-3."
        ),
    )
    assert_error(project, "options.yaml", "recommendation.why", "И-3", "источник вида «синтетика»")


def test_answer_to_the_objection_may_not_name_a_synthetic_claim(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: d["recommendation"].update(
            answer="Возражение снимает модельный респондент (У-7): защита нужна."
        ),
    )
    assert_error(project, "options.yaml", "recommendation.answer", "У-7 — синтетика")


def test_the_expected_effect_of_a_decision_may_not_name_a_synthetic_claim(project):
    edit_yaml(
        project,
        "decisions.yaml",
        lambda d: d["decisions"][0].update(expected="Доля оплат вырастет: респонденты подтвердили (У-7)"),
    )
    assert_error(project, "decisions.yaml", "expected", "У-7 — синтетика")


def test_the_text_of_a_promise_may_not_name_the_session(project):
    edit_yaml(
        project, "promises.yaml", lambda d: d["promises"][0].update(text="Обещаем то, что назвали в И-3")
    )
    assert_error(project, "promises.yaml", "И-3", "источник вида «синтетика»")


def test_a_text_cell_of_the_comparison_may_not_name_a_derived_claim(project):
    put_claims(project, hypothesis("У-8", ["У-7"]))

    def change(d: dict[str, Any]) -> None:
        cell = d["options"][0]["comparison"]["cost"]
        cell["text"] = cell.get("text", "") + " Это следует из У-8."

    edit_yaml(project, "options.yaml", change)
    assert_error(project, "options.yaml", "У-8 опирается только на синтетику")


def test_a_real_session_number_in_a_text_is_not_confused_with_a_synthetic_one(project):
    edit_yaml(
        project,
        "options.yaml",
        lambda d: d["recommendation"].update(answer="Данные из источника И-1 это подтверждают."),
    )
    assert errors_of(project) == []


def test_names_of_hypothesis_fields_are_an_exception_by_field_name():
    class Body(BaseModel):
        hypothesis: str = "Пока только на сессии И-3 и У-7: не проверена на людях"
        text: str = "Сказано в И-3"

    class Registry:
        exists = True
        ids: set[str] | None = set()
        file = "x.yaml"

    class Fake:
        by_id: dict[str, Any] = {"У-7": object()}
        synthetic = {"У-7": "direct"}
        synthetic_sources = {"И-3"}

    errors: list[str] = []
    project_check._check_text_refs(
        (("options.yaml", (), Body()),),
        Fake(),  # type: ignore[arg-type]
        Registry(),  # type: ignore[arg-type]
        Registry(),  # type: ignore[arg-type]
        errors,
        synthetic_files=frozenset({"options.yaml"}),
    )
    assert len(errors) == 1 and "поле text" in errors[0] and "И-3" in errors[0]


def test_files_outside_the_three_may_name_the_session(project):
    edit_yaml(project, "plan.yaml", lambda d: d["questions"][0].update(text="Что показала сессия И-3?"))
    assert not any("plan.yaml" in e and "синтетика" in e for e in errors_of(project))


# --- claim add: предупреждения -----------------------------------------------------------------------


def test_claim_add_warns_about_a_synthetic_text_that_reads_as_a_conclusion_about_people(
    session, cli, tmp_path
):
    from test_synth import LINE_ANSWER_1

    data = claim_in("И-2", LINE_ANSWER_1)
    data["text"] = "Пользователи тратят на заказ обеда двадцать минут и жалуются на это"
    code, out, err = add_claim(cli, tmp_path, data)
    assert code == 0, err
    assert "модельного респондента или персону" in out


def test_claim_add_does_not_warn_when_the_respondent_is_named(session, cli, tmp_path):
    from test_synth import LINE_ANSWER_1

    code, out, err = add_claim(cli, tmp_path, claim_in("И-2", LINE_ANSWER_1))
    assert code == 0, err
    assert "модельного респондента или персону" not in out
    assert claims_of(session)[0]["type"] == "синтетика"


def test_claim_add_warns_that_a_hypothesis_on_synthetic_is_not_checked_on_people(session, cli, tmp_path):
    from test_synth import LINE_ANSWER_1

    assert add_claim(cli, tmp_path, claim_in("И-2", LINE_ANSWER_1))[0] == 0
    hyp = {
        "text": "Офис-менеджеры чаще ждут курьера, чем это признают",
        "type": "гипотеза",
        "subject": "ожидание курьера",
        "basis": ["У-1"],
        "check_method": "спросить десять офис-менеджеров",
    }
    code, out, err = add_claim(cli, tmp_path, hyp)
    assert code == 0, err
    assert "не проверена на людях" in out


def test_the_project_stays_clean_for_the_old_sample():
    assert check_project(FIXTURE) == []
