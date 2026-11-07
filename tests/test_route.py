"""`strateg route`: модель и effort роли, профиль, повышение и его предел, события журнала (ADR-0008)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from helpers import COMPLETE, journal_of
from strateg import routing
from strateg.project import CliError, UsageError
from strateg.project_check import check_project

ROOT = Path(__file__).resolve().parent.parent
TABLE = yaml.safe_load((ROOT / "config" / "routing.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def proj(tmp_path, clock) -> Path:
    clock.advance(7200)
    shutil.copytree(COMPLETE, tmp_path / "p")
    return tmp_path / "p"


def route(cli, proj, *args):
    code, out, err = cli("route", *args, "--project", str(proj), "--json")
    return code, (json.loads(out) if out.strip().startswith("{") else out), err


def picks(proj, role: str | None = None) -> list[dict]:
    return [
        e for e in journal_of(proj) if e["kind"] == "выбор модели" and (role is None or e["role"] == role)
    ]


# --- таблица config/routing.yaml ------------------------------------------------


def test_table_matches_the_decision():
    assert TABLE["schema_version"] == 1
    assert TABLE["levels"] == {"сильная": "opus", "средняя": "sonnet", "дешёвая": "haiku"}
    roles = TABLE["roles"]
    assert set(roles) == {"researcher", "critic", "classifier", "respondent"}
    assert roles["researcher"] == {"level": "средняя", "effort": "medium", "depth": 3}
    assert roles["critic"]["level"] == "средняя" and roles["critic"]["effort"] == "high"
    assert roles["critic"]["upgrade"] == {
        "when": "проверка полноты не пройдена после доработки",
        "to": "сильная",
        "times": 1,
    }
    assert roles["classifier"]["level"] == "дешёвая" and roles["classifier"]["effort"] == "low"
    assert roles["classifier"]["upgrade"] == {"when": "ответ «не уверен»", "to": "средняя", "times": 1}
    assert roles["respondent"] == {"level": "средняя", "effort": "medium"}
    assert TABLE["profiles"] == {
        "экономнее": {"researcher": {"depth": 2}},
        "глубже": {"researcher": {"depth": 5}, "critic": {"level": "сильная"}},
    }


def test_table_loads_through_the_model():
    table = routing.load_routing()
    assert table.levels["сильная"] == "opus"
    assert routing.effective(table, "researcher", "обычный").depth == 3
    assert routing.effective(table, "researcher", "экономнее").depth == 2
    assert routing.effective(table, "critic", "глубже").model == "opus"
    assert routing.effective(table, "critic", "экономнее").model == "sonnet"


@pytest.fixture
def config(tmp_path, monkeypatch):
    def put(mutate) -> None:
        data = yaml.safe_load((ROOT / "config" / "routing.yaml").read_text(encoding="utf-8"))
        mutate(data)
        folder = tmp_path / "cfg"
        folder.mkdir(exist_ok=True)
        (folder / "routing.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        monkeypatch.setenv("STRATEG_CONFIG_DIR", str(folder))

    return put


def test_typo_in_level_is_explained(config):
    config(lambda d: d["roles"]["critic"].update(level="сильна"))
    with pytest.raises(CliError) as err:
        routing.load_routing()
    text = " ".join(err.value.details)
    assert "roles.critic.level" in text and "«сильная»" in text and "«средняя»" in text


def test_typo_in_profile_role_is_explained(config):
    config(lambda d: d["profiles"]["глубже"].update(critik={"level": "сильная"}))
    with pytest.raises(CliError) as err:
        routing.load_routing()
    text = " ".join(err.value.details)
    assert "critik" in text and "critic" in text and "глубже" in text


def test_typo_in_field_profile_name_and_levels(config):
    config(lambda d: d["roles"]["critic"].update(efort="high"))
    with pytest.raises(CliError) as err:
        routing.load_routing()
    assert "efort" in " ".join(err.value.details)
    config(lambda d: d["profiles"].update(быстрее={"researcher": {"depth": 1}}))
    with pytest.raises(CliError):
        routing.load_routing()
    config(lambda d: d["levels"].pop("дешёвая"))
    with pytest.raises(CliError) as err:
        routing.load_routing()
    assert "дешёвая" in " ".join(err.value.details)


def test_profile_that_changes_nothing_is_refused(config):
    config(lambda d: d["profiles"].update(экономнее={"researcher": {}}))
    with pytest.raises(CliError):
        routing.load_routing()


def test_missing_table_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEG_CONFIG_DIR", str(tmp_path / "нет"))
    with pytest.raises(CliError) as err:
        routing.load_routing()
    assert "routing.yaml" in err.value.message


# --- выбор роли ---------------------------------------------------------------


def test_route_names_model_effort_and_writes_the_event(cli, proj):
    code, data, err = route(cli, proj, "critic", "--reason", "проверка полноты")
    assert code == 0, err
    assert data["model"] == "sonnet" and data["level"] == "средняя" and data["effort"] == "high"
    assert data["profile"] == "обычный" and data["upgrade"] is False
    event = picks(proj, "critic")[-1]
    assert (event["role"], event["model"], event["level"], event["effort"]) == (
        "critic",
        "sonnet",
        "средняя",
        "high",
    )
    assert "проверка полноты" in event["message"] and event["actor"] == "CLI"
    assert check_project(proj) == []


def test_route_researcher_has_depth_and_text_output(cli, proj):
    code, data, _ = route(cli, proj, "researcher")
    assert data["depth"] == 3
    code, out, _ = cli("route", "classifier", "--project", str(proj))
    assert code == 0 and "haiku" in out and "low" in out and "дешёвая" in out
    assert "по таблице маршрутов" in picks(proj, "classifier")[-1]["message"]


def test_unknown_role_is_a_usage_error_listing_the_roles(cli, proj):
    code, out, err = cli("route", "critik", "--project", str(proj))
    assert code == 2
    assert "critik" in err and "critic" in err and "researcher" in err
    assert picks(proj, "critik") == []


def test_role_and_profile_together_or_neither_are_usage_errors(cli, proj):
    assert cli("route", "critic", "--profile", "глубже", "--project", str(proj))[0] == 2
    assert cli("route", "--project", str(proj))[0] == 2
    assert cli("route", "critic", "--profile", "быстрее", "--project", str(proj))[0] == 2


# --- профиль ---------------------------------------------------------------------


def test_profile_changes_state_journal_and_following_choices(cli, proj):
    code, data, err = route(cli, proj, "--profile", "глубже")
    assert code == 0, err
    assert data["profile"] == "глубже" and data["changed"] is True
    state = yaml.safe_load((proj / "state.yaml").read_text(encoding="utf-8"))
    assert state["profile"] == "глубже"
    event = journal_of(proj)[-1]
    assert event["name"] == "смена профиля" and "обычный -> глубже" in event["message"]
    code, data, _ = route(cli, proj, "critic")
    assert data["model"] == "opus" and data["profile"] == "глубже"
    code, data, _ = route(cli, proj, "researcher")
    assert data["depth"] == 5
    assert check_project(proj) == []


def test_economy_profile_and_return_to_normal(cli, proj):
    route(cli, proj, "--profile", "экономнее")
    assert route(cli, proj, "researcher")[1]["depth"] == 2
    route(cli, proj, "--profile", "обычный")
    assert route(cli, proj, "researcher")[1]["depth"] == 3
    names = [e.get("name") for e in journal_of(proj)]
    assert names.count("смена профиля") == 2
    assert check_project(proj) == []


def test_same_profile_again_changes_nothing(cli, proj):
    route(cli, proj, "--profile", "глубже")
    events = len(journal_of(proj))
    code, data, _ = route(cli, proj, "--profile", "глубже")
    assert code == 0 and data["changed"] is False and len(journal_of(proj)) == events
    code, data, _ = route(cli, proj, "--profile", "обычный")
    assert data["changed"] is True
    code, data, _ = route(cli, proj, "--profile", "обычный")
    assert data["changed"] is False


# --- повышение --------------------------------------------------------------------


def test_upgrade_goes_to_the_stronger_model_and_is_logged(cli, proj):
    code, data, err = route(cli, proj, "critic", "--upgrade", "--reason", "после доработки не пройдено")
    assert code == 0, err
    assert data["model"] == "opus" and data["level"] == "сильная" and data["upgrade"] is True
    event = picks(proj, "critic")[-1]
    assert event["name"] == "повышение" and event["model"] == "opus" and event["level"] == "сильная"
    assert "средняя -> сильная" in event["message"]
    assert "проверка полноты не пройдена после доработки" in event["message"]
    assert "после доработки не пройдено" in event["message"]
    assert check_project(proj) == []


def test_upgrade_is_limited_to_times_per_role_and_step(cli, proj):
    assert route(cli, proj, "critic", "--upgrade")[0] == 0
    before = len(journal_of(proj))
    code, out, err = cli("route", "critic", "--upgrade", "--project", str(proj))
    assert code == 1 and "предел" in err and "1 раз" in err
    assert len(journal_of(proj)) == before
    # другая роль на том же шаге — отдельный счёт
    assert route(cli, proj, "classifier", "--upgrade")[0] == 0
    # обычный выбор роли не считается повышением
    assert route(cli, proj, "critic")[0] == 0
    # на другом шаге счёт свой
    assert cli("step", "start", "6", "--project", str(proj))[0] == 0
    assert route(cli, proj, "critic", "--upgrade")[0] == 0
    assert len([e for e in picks(proj, "critic") if e["name"] == "повышение"]) == 2


def test_upgrade_limit_follows_times_from_the_table(cli, proj, config):
    config(lambda d: d["roles"]["classifier"]["upgrade"].update(to="сильная", times=2))
    assert route(cli, proj, "classifier", "--upgrade")[1]["model"] == "opus"
    assert route(cli, proj, "classifier", "--upgrade")[0] == 0
    code, _, err = cli("route", "classifier", "--upgrade", "--project", str(proj))
    assert code == 1 and "2 раз из 2" in err


def test_role_without_upgrade_rule_is_refused(cli, proj):
    code, _, err = cli("route", "researcher", "--upgrade", "--project", str(proj))
    assert code == 1 and "нет условия повышения" in err
    assert picks(proj, "researcher") == []


def test_nothing_to_upgrade_when_profile_already_strongest(cli, proj):
    route(cli, proj, "--profile", "глубже")
    code, _, err = cli("route", "critic", "--upgrade", "--project", str(proj))
    assert code == 1 and "повышать некуда" in err


def test_route_event_is_valid_for_the_journal_model():
    from strateg.models import JournalEvent

    with pytest.raises(Exception, match="role и model"):
        JournalEvent.model_validate(
            {
                "schema_version": 1,
                "time": "2026-11-06T10:00:00+03:00",
                "actor": "CLI",
                "kind": "выбор модели",
                "step": "1",
            }
        )
    with pytest.raises(Exception, match="level и effort"):
        JournalEvent.model_validate(
            {
                "schema_version": 1,
                "time": "2026-11-06T10:00:00+03:00",
                "actor": "CLI",
                "kind": "ошибка",
                "step": "1",
                "message": "x",
                "level": "средняя",
            }
        )


def test_usage_error_class_is_used_for_unknown_role():
    table = routing.load_routing()
    with pytest.raises(UsageError):
        routing.effective(table, "unknown", "обычный")
