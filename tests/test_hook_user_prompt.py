"""Хук `UserPromptSubmit` (ADR-0009, ADR-0010): ответ человека на остановку — событие в журнале."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import journal_of, put_frame
from strateg import project as proj
from strateg.hooks import effects, runner
from strateg.project_check import check_project
from test_hooks_integration import FOLDER, new_demo, product, run_hook  # noqa: F401
from test_hooks_runner import env

PROMPT = "да, продолжай, секретное слово слива-42"


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


@pytest.fixture
def demo(cli, home):
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def prompt_event(**extra) -> bytes:
    data = {"hook_event_name": "UserPromptSubmit", "session_id": "s-1", "prompt": PROMPT, "cwd": ""}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def answers(root) -> list[dict]:
    return [e for e in journal_of(root) if e["kind"] == "ответ человека"]


def submit(e=None, **extra):
    return runner.run_user_prompt_submit(prompt_event(**extra), e or env())


def work_of(cli) -> float:
    code, out, _ = cli("status", "--json")
    return json.loads(out)["work_minutes"]


def stopped(cli, clock, demo, wait: int = 900) -> None:
    """Шаг 2б идёт 120 с, агент останавливается и ждёт ответа `wait` секунд."""
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(120)
    assert cli("stop", "--reason", "нужен ответ")[0] == 0
    clock.advance(wait)


# --- Сценарий потерь ---------------------------------------------------------------------------------------


def test_stop_then_answer_then_work_without_commands_keeps_all_the_work(cli, demo, clock):
    """Остановка → 900 с → ответ человека → 300 с работы без команд → `step done`: работа 120 + 300 с."""
    stopped(cli, clock, demo)
    result = submit()
    assert result.stdout == "" and result.exit_code == 0
    (answer,) = answers(demo)
    assert answer["actor"] == "хук" and answer["time"] == clock.value.isoformat()
    clock.advance(300)  # агент работает: ни одной команды CLI
    done = json.loads(cli("step", "done", "2б", "--json")[1])
    assert done["duration_s"] == 420
    assert work_of(cli) == pytest.approx(7.0)
    assert check_project(demo) == []
    assert proj.Project.load(demo).outside == []


def test_without_the_hook_the_wait_ends_on_the_first_event_of_the_project_as_before(cli, demo, clock):
    stopped(cli, clock, demo)
    clock.advance(300)
    assert json.loads(cli("step", "done", "2б", "--json")[1])["duration_s"] == 120  # 300 с ушли в ожидание


def test_the_answer_is_a_move_of_the_project_and_ends_the_pause(cli, demo, clock):
    stopped(cli, clock, demo)
    assert json.loads(cli("status", "--brief", "--peek", "--json")[1])["paused"] == "остановка"
    submit()
    brief = json.loads(cli("status", "--brief", "--peek", "--json")[1])
    assert brief["paused"] is None and brief["step_in_progress"] is True
    assert proj.pause_kind(proj.Project.load(demo).journal) is None
    ends = [e for e in proj.progress_events(proj.Project.load(demo).journal) if e.kind == proj.HUMAN_ANSWER]
    assert len(ends) == 1


def test_the_text_of_the_message_never_reaches_the_journal(cli, demo, clock):
    stopped(cli, clock, demo)
    submit()
    text = (demo / "journal.jsonl").read_text(encoding="utf-8")
    assert "слива-42" not in text and "продолжай" not in text
    assert "слива-42" not in (demo / "state.yaml").read_text(encoding="utf-8")
    (answer,) = answers(demo)
    assert set(answer) == {"schema_version", "time", "actor", "kind", "step", "message"}


# --- Когда хук молчит -------------------------------------------------------------------------------------


def test_hook_without_a_stop_writes_nothing(cli, demo, clock):
    cli("step", "start", "2б")
    clock.advance(30)
    before = journal_of(demo)
    assert submit().stdout == ""
    assert journal_of(demo) == before and answers(demo) == []


def test_hook_on_open_gates_writes_nothing(cli, demo, clock):
    cli("step", "start", "1")
    put_frame(cli)
    assert cli("gate", "open", "В1", "--questions", "1")[0] == 0
    clock.advance(60)
    before = journal_of(demo)
    submit()
    assert journal_of(demo) == before  # на воротах ожидание кончает ответ с `answered_at`


def test_open_gates_after_an_answered_stop_still_get_no_event(cli, demo, clock):
    """В журнале уже есть остановка, но сейчас проект ждёт ворот: хук молчит."""
    cli("step", "start", "1")
    put_frame(cli)
    clock.advance(30)
    assert cli("stop", "--reason", "нужен ответ")[0] == 0
    clock.advance(30)
    submit()
    assert len(answers(demo)) == 1
    clock.advance(30)
    assert cli("gate", "open", "В1", "--questions", "1")[0] == 0
    clock.advance(30)
    before = journal_of(demo)
    submit()
    assert journal_of(demo) == before
    assert proj.pause_kind(proj.Project.load(demo).journal) == proj.PAUSE_GATE


def test_a_repeated_message_does_not_write_a_second_answer(cli, demo, clock):
    stopped(cli, clock, demo)
    for _ in range(3):
        submit()
        clock.advance(5)
    assert len(answers(demo)) == 1
    assert check_project(demo) == []


def test_a_new_stop_waits_for_its_own_answer(cli, demo, clock):
    stopped(cli, clock, demo)
    submit()
    clock.advance(10)
    assert cli("stop", "--reason", "второй вопрос")[0] == 0
    clock.advance(500)
    assert json.loads(cli("status", "--brief", "--peek", "--json")[1])["paused"] == "остановка"
    submit()
    assert len(answers(demo)) == 2
    clock.advance(40)
    assert json.loads(cli("step", "done", "2б", "--json")[1])["duration_s"] == 120 + 10 + 40


def test_a_hook_event_between_the_stop_and_the_message_does_not_count_as_the_answer(cli, demo, clock):
    stopped(cli, clock, demo, wait=10)
    assert effects.append_event(demo, effects.EventSpec("учёт недоступен", "учёт не удался", name="s1"))
    assert proj.pause_kind(proj.Project.load(demo).journal) == "остановка"
    submit()
    assert len(answers(demo)) == 1


def test_no_project_no_event_and_no_output(home, tmp_path):
    result = submit()
    assert result == runner.HookResult()
    assert not home.exists() or not any(home.rglob("journal.jsonl"))


def test_hooks_off_writes_nothing(cli, demo, clock):
    stopped(cli, clock, demo)
    assert submit(e=env(STRATEG_HOOKS="0")).stdout == ""
    assert answers(demo) == []


def test_foreign_environment_writes_nothing(cli, demo, clock):
    stopped(cli, clock, demo)
    submit(conversation_id="c-1", cursor_version="1.0")  # Cursor: события другие, хуков Claude Code нет
    assert answers(demo) == []


@pytest.mark.parametrize("raw", [b"", b"\xff\xfe garbage", b"[]", b'{"hook_event_name": 1}'])
def test_garbage_input_gives_exit_zero_and_no_output(cli, demo, clock, raw):
    stopped(cli, clock, demo)
    result = runner.run_user_prompt_submit(raw, env())
    assert result == runner.HookResult()
    assert answers(demo) == []


def test_a_failure_inside_the_hook_gives_exit_zero_and_no_output(cli, demo, clock, monkeypatch):
    stopped(cli, clock, demo)

    def broken(*_a, **_k):
        raise OSError("диск недоступен")

    monkeypatch.setattr(effects, "append_event", broken)
    result = submit()
    assert result == runner.HookResult() and answers(demo) == []


def test_the_event_is_fast_to_find_when_there_is_no_stop_in_the_journal(cli, demo, clock, monkeypatch):
    """Без остановки в журнале модели проекта для решения не нужны."""
    cli("step", "start", "2б")
    called = []
    monkeypatch.setattr(proj, "pause_kind", lambda events: called.append(1))
    submit()
    assert called == []


# --- Скрипт-обёртка и настройки ----------------------------------------------------------------------------


def test_script_writes_one_answer_prints_nothing_and_survives_garbage(product):  # noqa: F811
    new_demo(product)
    root = product / "projects" / "demo"
    subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "strateg.cli", "stop", "--reason", "нужен ответ"],
        env={**os.environ, "PYTHONPATH": str(product / "src"), "PYTHONUTF8": "1",
             "STRATEG_PROJECTS_DIR": str(product / "projects")},
        cwd=product,
        capture_output=True,
        check=True,
    )  # fmt: skip
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": PROMPT, "cwd": str(product)}
    proc = run_hook(product, "user_prompt_submit.py", event)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    assert [json.loads(line)["kind"] for line in (root / "journal.jsonl").read_text("utf-8").splitlines()][
        -1
    ] == ("ответ человека")
    assert run_hook(product, "user_prompt_submit.py", event).stdout == b""
    assert len(answers(root)) == 1
    assert PROMPT not in (root / "journal.jsonl").read_text(encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, "-X", "utf8", str(product / ".claude" / "hooks" / "user_prompt_submit.py")],
        input=b"\xff\xfe garbage",
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(product / "src")},
    )
    assert bad.returncode == 0 and bad.stdout == b"" and bad.stderr == b""


def test_script_without_a_project_prints_nothing(product):  # noqa: F811
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": PROMPT, "cwd": str(product)}
    proc = run_hook(product, "user_prompt_submit.py", event)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    assert FOLDER and not list(Path(product / "projects").iterdir())


def test_human_answer_is_a_hook_event_with_a_message_in_the_model():
    from strateg.models import JournalEvent

    base = {
        "schema_version": 1,
        "time": "2026-11-08T22:00:00+03:00",
        "actor": "хук",
        "kind": "ответ человека",
        "step": "2б",
    }
    with pytest.raises(ValueError, match="пояснения"):
        JournalEvent.model_validate(base)
    assert JournalEvent.model_validate({**base, "message": "человек ответил"}).kind == "ответ человека"
