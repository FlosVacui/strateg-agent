"""Внеплановая остановка видна в `status` (--json, --line, --brief) и после `step done`; ответ её снимает."""

from __future__ import annotations

import json

import pytest

from helpers import journal_of
from strateg import project as proj
from strateg.hooks import effects
from strateg.hooks.rules import EventSpec

WAIT_STOP = "ждём ответ человека на внеплановую остановку"


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


def run(cli, *argv: str) -> dict:
    code, out, err = cli(*argv, "--json")
    assert code == 0, err or out
    return json.loads(out)


def setup(cli, clock, *, step_closed: bool) -> None:
    """Шаг идёт, затем остановка; при `step_closed` шаг закрыт до остановки: `step done`, `stop`."""
    assert cli("new", "demo")[0] == 0
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(120)
    if step_closed:
        assert cli("step", "done", "2б")[0] == 0
        clock.advance(5)
    assert cli("stop", "--reason", "нужен ответ")[0] == 0
    clock.advance(900)


@pytest.fixture
def root(home):
    return home / "demo"


def answer_by_hook(root, clock) -> None:
    assert effects.append_event(root, EventSpec(effects.HUMAN_ANSWER, "человек ответил"))
    clock.advance(10)


@pytest.mark.parametrize("step_closed", [False, True], ids=["шаг идёт", "шаг закрыт"])
def test_an_unanswered_stop_is_seen_everywhere(cli, root, clock, step_closed):
    setup(cli, clock, step_closed=step_closed)
    d = run(cli, "status")
    assert d["paused"] == "остановка" and d["step_in_progress"] is (not step_closed)
    line = cli("status", "--line")[1].strip()
    assert "на паузе: " + WAIT_STOP in line and "\n" not in line
    before = journal_of(root)
    b = run(cli, "status", "--brief")
    assert b["paused"] == "остановка" and b["interrupted"] == []
    code, out, _ = cli("status", "--brief")
    assert code == 0 and "на паузе: " + WAIT_STOP in out and "Прервано" not in out
    assert journal_of(root) == before  # сводка ничего не прерывает и не пишет
    assert "на паузе: " + WAIT_STOP in cli("status")[1]


@pytest.mark.parametrize("step_closed", [False, True], ids=["шаг идёт", "шаг закрыт"])
@pytest.mark.parametrize("how", ["хук", "команда"])
def test_the_answer_lifts_the_stop(cli, root, clock, step_closed, how):
    setup(cli, clock, step_closed=step_closed)
    if how == "хук":
        answer_by_hook(root, clock)
    else:
        assert cli("budget", "set", "--usd", "9")[0] == 0
        clock.advance(10)
    assert run(cli, "status")["paused"] is None
    assert "на паузе" not in cli("status", "--line")[1]
    b = run(cli, "status", "--brief", "--peek")
    assert b["paused"] is None
    assert "Ждём ответ" not in cli("status", "--brief", "--peek")[1]


def test_the_stop_after_step_done_is_not_reported_as_interrupted(cli, root, clock):
    setup(cli, clock, step_closed=True)
    out = cli("status", "--brief")[1]
    assert "Прервано" not in out and "прерванный" not in out
    assert [e["status"] for e in journal_of(root) if e.get("name") == "шаг 2б"] == ["начато", "выполнено"]


def test_the_stop_after_step_done_names_the_wait_in_the_step_line(cli, clock):
    setup(cli, clock, step_closed=True)
    assert f"Шаг: 2б (Сбор) · на паузе: {WAIT_STOP}" in cli("status", "--brief")[1]


def test_a_hook_event_after_a_closed_step_does_not_hide_the_stop(cli, root, clock):
    setup(cli, clock, step_closed=True)
    assert effects.append_event(root, EventSpec("бюджет", "бюджет: осталось 20 %", role="researcher"))
    assert run(cli, "status")["paused"] == "остановка"


@pytest.mark.parametrize("step_closed", [False, True], ids=["шаг идёт", "шаг закрыт"])
def test_waiting_after_a_stop_is_not_work(cli, clock, step_closed):
    setup(cli, clock, step_closed=step_closed)
    d = run(cli, "status")
    assert d["work_minutes"] == pytest.approx(2.0)  # 120 с до остановки; 900 с ожидания не в счёт
    assert cli("status", "--line")[1].strip().endswith("2 из 60 мин")
    clock.advance(3600)
    assert run(cli, "status")["work_minutes"] == pytest.approx(2.0)


def test_the_wait_ends_with_the_answer_after_a_closed_step(cli, root, clock):
    from strateg.models import JournalEvent

    setup(cli, clock, step_closed=True)
    answer_by_hook(root, clock)
    events = [JournalEvent.model_validate(e) for e in journal_of(root)]
    ((start, end),) = proj.stop_waits(events)
    assert end is not None and (end - start).total_seconds() == pytest.approx(900)
    assert proj.pause_kind(events) is None


def test_gates_are_unchanged(cli, clock):
    from helpers import put_frame

    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    clock.advance(600)
    d = run(cli, "status")
    assert d["paused"] == "ворота" and d["waiting_gates"] == ["В1"]
    assert "на паузе: ждём ответа на В1" in cli("status", "--brief", "--peek")[1]
    assert "на паузе" not in cli("status", "--line")[1]  # строка показывает только остановку
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    assert run(cli, "status")["paused"] is None
    cli("step", "done", "1")
    assert run(cli, "status")["paused"] is None


def test_open_gates_after_step_done_are_not_a_pause_as_before(cli, clock):
    from helpers import put_frame

    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    cli("step", "done", "1")
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    d = run(cli, "status")
    assert d["paused"] is None and d["waiting_gates"] == ["В1"]


def stopped_after_done(cli, clock) -> None:
    setup(cli, clock, step_closed=True)
    assert run(cli, "status")["paused"] == "остановка"


@pytest.mark.parametrize(
    "argv",
    [
        ("route", "critic", "--reason", "повод"),
        ("event", "error", "--message", "сбой"),
        ("step", "start", "3"),
    ],
    ids=["route", "event error", "step start"],
)
def test_a_command_that_writes_a_progress_event_lifts_the_stop(cli, clock, argv):
    """Агент пошёл дальше: остановка уже не настоящая."""
    stopped_after_done(cli, clock)
    assert cli(*argv)[0] == 0
    assert run(cli, "status")["paused"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ("status",),
        ("status", "--line"),
        ("status", "--brief", "--peek"),
        ("budget", "check"),
        ("check",),
        ("claim", "list"),
    ],
    ids=lambda a: " ".join(a),
)
def test_a_read_only_command_does_not_lift_the_stop(cli, root, clock, argv):
    stopped_after_done(cli, clock)
    before = journal_of(root)
    cli(*argv)
    assert run(cli, "status")["paused"] == "остановка"
    assert [e for e in journal_of(root)[len(before) :] if e["kind"] != "изменено вне агента"] == []
