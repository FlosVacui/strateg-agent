"""Повышение критика вживую, командами CLI: провал полноты после доработки → `route critic --upgrade` →
вторая запись `rubric record --by критик` → журнал и `cost report` показывают обе попытки и модели."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers import COMPLETE, journal_of
from strateg.hooks import effects
from strateg.hooks.rules import EventSpec
from strateg.project_check import check_project
from test_rubric_record import answers, record


@pytest.fixture
def proj(tmp_path, clock) -> Path:
    clock.advance(7200)
    shutil.copytree(COMPLETE, tmp_path / "p")
    return tmp_path / "p"


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


def critic_done(root, agent_id):
    """След вызова критика, который пишет хук `SubagentStop`."""
    assert effects.append_event(
        root, EventSpec("роль выполнена", "роль critic завершила работу", agent_id, "critic")
    )


def route(cli, proj, *args):
    code, out, err = cli("route", "critic", *args, "--project", str(proj), "--json")
    assert code == 0, err
    return json.loads(out)


def records(proj) -> list[dict]:
    return [e for e in journal_of(proj) if e.get("name") == "strateg rubric record"]


def report(cli, proj) -> tuple[dict, str]:
    code, out, err = cli("cost", "report", "--project", str(proj), "--json")
    assert code == 0, err
    text = cli("cost", "report", "--project", str(proj))[1]
    return json.loads(out), text


FAILED = {"Б_1": "нет"}


def first_attempt(cli, proj, clock):
    assert route(cli, proj, "--reason", "полнота")["model"] == "sonnet"
    critic_done(proj, "a1")
    assert record(cli, proj, answers(**FAILED))[0] == 1  # не пройдено
    clock.advance(600)  # доработка
    up = route(cli, proj, "--upgrade", "--reason", "полнота не прошла после доработки")
    assert up["model"] == "opus" and up["upgrade"] is True
    critic_done(proj, "a2")
    clock.advance(60)


def test_failed_retry_on_the_strong_model_is_a_second_record_with_its_model(cli, proj, clock):
    first_attempt(cli, proj, clock)
    code, data, _ = record(cli, proj, answers(**FAILED))  # критик на opus дал те же ответы
    assert code == 1 and data["changed"] is True  # новая попытка записана, а не «без изменений»
    first, second = records(proj)
    assert "не пройдено" in first["message"] and "модель sonnet" in first["message"]
    assert "не пройдено" in second["message"] and "модель opus" in second["message"]
    d, text = report(cli, proj)
    (up,) = d["model_upgrades"]
    assert (up["role"], up["model"]) == ("critic", "opus")
    assert up["outcome"] == "повтор проверки полноты: не пройдено"
    runs = [(r["result"], r["by"], r["model"]) for r in d["completeness_runs"]]
    assert runs == [("не пройдено", "критик", "sonnet"), ("не пройдено", "критик", "opus")]
    assert "итог: повтор проверки полноты: не пройдено" in text
    assert "Проверки полноты:" in text and "не пройдено; критик, модель opus" in text
    # второго повтора нет: предел из таблицы
    code, _, err = cli("route", "critic", "--upgrade", "--project", str(proj))
    assert code == 1 and "предел" in err
    assert check_project(proj) == []


def test_passed_retry_is_the_outcome_of_the_upgrade(cli, proj, clock):
    first_attempt(cli, proj, clock)
    assert record(cli, proj, answers())[0] == 0
    d, text = report(cli, proj)
    assert d["model_upgrades"][0]["outcome"] == "повтор проверки полноты: пройдено"
    assert [r["result"] for r in d["completeness_runs"]] == ["не пройдено", "пройдено"]
    assert "пройдено; критик, модель opus" in text


def test_upgrade_without_a_retry_says_so(cli, proj, clock):
    first_attempt(cli, proj, clock)
    d, _ = report(cli, proj)
    assert d["model_upgrades"][0]["outcome"] == "повтора ещё нет"


def test_same_answers_without_an_upgrade_are_still_unchanged(cli, proj, clock):
    critic_done(proj, "a1")
    record(cli, proj, answers(**FAILED))
    clock.advance(60)
    code, data, _ = record(cli, proj, answers(**FAILED))
    assert data["changed"] is False and len(records(proj)) == 1


def test_agent_record_has_no_critic_model(cli, proj):
    route(cli, proj, "--reason", "полнота, исполняет основной агент")
    record(cli, proj, answers(), "--by", "агент")
    (r,) = report(cli, proj)[0]["completeness_runs"]
    assert r["by"] == "агент" and r["model"] is None
    assert "модель" not in records(proj)[0]["message"]
