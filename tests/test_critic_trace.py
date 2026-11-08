"""След роли критика: `rubric record --by критик` без подтверждённого вызова не независима."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers import COMPLETE, journal_of
from strateg import project as projlib
from strateg.hooks import effects, runner
from strateg.hooks.rules import EventSpec
from strateg.models import CompletenessFile
from strateg.project_check import check_project
from test_hooks_runner import env
from test_rubric_record import answers, record, saved
from transcripts import agent_file, session_files, write


@pytest.fixture
def proj(tmp_path, clock) -> Path:
    clock.advance(7200)  # журнал эталона кончается позже стартовых часов теста
    shutil.copytree(COMPLETE, tmp_path / "p")
    return tmp_path / "p"


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


def role_done(root, role="critic", agent_id="a1"):
    assert effects.append_event(
        root, EventSpec("роль выполнена", f"роль {role} завершила работу", agent_id, role)
    )


UNCONFIRMED = "Вызов критика не подтверждён"


def test_no_trace_of_the_critic_is_said_in_the_file_the_output_and_the_journal(cli, proj):
    code, out, err = cli(
        "rubric",
        "record",
        "--project",
        str(proj),
        "--file",
        "-",
        "--by",
        "критик",
        stdin=json.dumps(answers()),
    )
    assert code == 0, err
    assert UNCONFIRMED in out and "не считается независимой" in out
    assert saved(proj)["critic_call"] == "не подтверждён" and saved(proj)["by"] == "критик"
    action = [e for e in journal_of(proj) if e.get("name") == "strateg rubric record"][-1]
    assert "вызов критика не подтверждён" in action["message"]
    assert check_project(proj) == []


def test_json_output_does_not_pass_it_off_as_independent(cli, proj):
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert code == 0 and data["independent"] is False and data["critic_call"] == "не подтверждён"


def test_trace_of_the_critic_after_the_start_confirms_the_call(cli, proj):
    role_done(proj)
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert code == 0 and data["independent"] is True and data["critic_call"] == "подтверждён"
    assert saved(proj)["critic_call"] == "подтверждён"
    code, out, _ = cli(
        "rubric",
        "record",
        "--project",
        str(proj),
        "--file",
        "-",
        "--by",
        "критик",
        stdin=json.dumps(answers()),
    )
    assert UNCONFIRMED not in out
    assert check_project(proj) == []


@pytest.mark.parametrize("role", ["researcher", "classifier", "основной"])
def test_trace_of_another_role_is_not_a_critic_call(cli, proj, role):
    role_done(proj, role=role)
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert data["critic_call"] == "не подтверждён" and data["independent"] is False


def test_trace_from_another_step_is_not_a_critic_call(cli, proj, clock):
    """Критик возражал на варианты на шаге 4: на проверке полноты (шаг 7) это не вызов критика."""
    p = projlib.Project.load(proj)
    p.add_event(
        p.event(
            step="4", actor="хук", kind="роль выполнена", role="critic", name="old", message="роль critic"
        )
    )
    p.save_state()
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert data["critic_call"] == "не подтверждён"


def test_a_second_record_with_new_answers_needs_a_new_call_of_the_critic(cli, proj, clock):
    role_done(proj)
    assert record(cli, proj, answers(), "--by", "критик")[1]["critic_call"] == "подтверждён"
    clock.advance(60)
    code, data, _ = record(cli, proj, answers(Б_1="нет"), "--by", "критик")
    assert data["critic_call"] == "не подтверждён" and data["independent"] is False
    clock.advance(60)
    role_done(proj, agent_id="a2")
    code, data, _ = record(cli, proj, answers(Б_1="нет"), "--by", "критик")
    assert data["critic_call"] == "подтверждён"


def test_repeating_the_same_answers_keeps_the_confirmation(cli, proj, clock):
    role_done(proj)
    record(cli, proj, answers(), "--by", "критик")
    clock.advance(60)
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert data["changed"] is False and data["critic_call"] == "подтверждён" and data["independent"] is True
    assert saved(proj)["critic_call"] == "подтверждён"


def test_check_by_the_agent_is_not_a_critic_claim_and_has_no_field(cli, proj):
    code, out, _ = cli(
        "rubric",
        "record",
        "--project",
        str(proj),
        "--file",
        "-",
        "--by",
        "агент",
        stdin=json.dumps(answers()),
    )
    assert code == 0 and UNCONFIRMED not in out and "не независимая" in out
    assert "critic_call" not in saved(proj)


def test_the_field_is_only_valid_for_the_critic(cli, proj):
    record(cli, proj, answers(), "--by", "агент")
    data = {**saved(proj), "critic_call": "подтверждён"}
    with pytest.raises(ValueError, match="critic_call"):
        CompletenessFile.model_validate(data)


def test_subagent_stop_hook_leaves_the_trace_that_the_command_looks_for(cli, proj, tmp_path):
    """Сквозной путь: хук `SubagentStop` для `critic` — и запись проверки видит вызов."""
    main, folder = session_files(tmp_path / "claude", "sess-1")
    write(main)
    path = agent_file(folder, "c1")
    raw = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-1",
            "transcript_path": str(main),
            "agent_id": "c1",
            "agent_type": "critic",
            "agent_transcript_path": str(path),
        }
    ).encode("utf-8")
    runner.run_subagent_stop(raw, env(STRATEG_PROJECT=str(proj)))
    code, data, _ = record(cli, proj, answers(), "--by", "критик")
    assert data["critic_call"] == "подтверждён" and data["independent"] is True
    assert check_project(proj) == []


def test_a_resumed_critic_leaves_a_new_trace_with_the_same_agent_id(cli, proj, clock):
    """Критика возобновили сообщением: тот же `agent_id`, второе завершение. Без нового события вторая
    запись проверки считалась бы сделанной без критика."""
    role_done(proj, agent_id="a1")
    assert record(cli, proj, answers(), "--by", "критик")[1]["critic_call"] == "подтверждён"
    clock.advance(60)
    code, data, _ = record(cli, proj, answers(Б_1="нет"), "--by", "критик")
    assert data["critic_call"] == "не подтверждён"
    clock.advance(60)
    role_done(proj, agent_id="a1")  # то же имя агента, новое завершение
    code, data, _ = record(cli, proj, answers(Б_1="нет"), "--by", "критик")
    assert data["critic_call"] == "подтверждён" and data["independent"] is True
    names = [e["name"] for e in journal_of(proj) if e["kind"] == "роль выполнена"]
    assert names == ["a1", "a1#2"]
    assert check_project(proj) == []


def test_the_same_completion_delivered_twice_in_one_moment_is_one_trace(proj, clock):
    assert effects.append_event(
        proj, EventSpec("роль выполнена", "роль critic завершила работу", "a1", "critic")
    )
    assert not effects.append_event(
        proj, EventSpec("роль выполнена", "роль critic завершила работу", "a1", "critic")
    )
    clock.advance(1)
    assert not effects.append_event(
        proj, EventSpec("роль выполнена", "роль critic завершила работу", "a1", "critic")
    )
    clock.advance(10)
    assert effects.append_event(
        proj, EventSpec("роль выполнена", "роль critic завершила работу", "a1", "critic")
    )
    names = [e["name"] for e in journal_of(proj) if e["kind"] == "роль выполнена"]
    assert names == ["a1", "a1#2"]
