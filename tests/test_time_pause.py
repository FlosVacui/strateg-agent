"""Время работы без потерь: пауза шага на воротах и остановке, длительность у «прервано», время по часам."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from helpers import edit_journal, journal_of, put_frame, rehash, tree_hashes
from strateg import project as proj
from strateg.hooks import effects
from strateg.hooks.rules import EventSpec
from strateg.project_check import check_project

ZONE = dt.timezone(dt.timedelta(hours=3))

# События, которые пишут хуки посреди работы. Ход проекта они не двигают (кроме задания исследователю).
HOOK_EVENTS = {
    "роль выполнена": EventSpec(
        "роль выполнена", "роль researcher завершила работу", name="a1", role="researcher"
    ),
    "учёт недоступен": EventSpec(
        "учёт недоступен", "учёт стоимости по транскриптам не удался: OSError", name="s1"
    ),
    "отказ хука": EventSpec("отказ хука", "запись вне outputs закрыта", name="Write", role="critic"),
    "бюджет": EventSpec("бюджет", "бюджет: осталось 20 %", role="researcher"),
    "граница прав не активна": EventSpec(
        "граница прав не активна", "вход хука не похож на Claude Code", name="s1"
    ),
}


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


def hook(root, kind: str) -> None:
    """Событие хука в журнале проекта, как его пишет `strateg.hooks`."""
    assert effects.append_event(root, HOOK_EVENTS[kind])


def at(clock, hms: str, day: int = 8) -> None:
    """Ставит часы проекта на время ЧЧ:ММ:СС ноября 2026 (пояс проекта)."""
    h, m, s = (int(x) for x in hms.split(":"))
    clock.value = dt.datetime(2026, 11, day, h, m, s, tzinfo=ZONE)


def run(cli, *argv: str) -> dict:
    code, out, err = cli(*argv, "--json")
    assert code == 0, err or out
    return json.loads(out)


def statuses(root, name: str) -> list[str]:
    return [e["status"] for e in journal_of(root) if e.get("name") == name]


def minutes(cli) -> float:
    return run(cli, "status")["work_minutes"]


# --- Прогон 08.11.2026: те же времена, что в журнале сквозного прогона ----------------------------------


def run_of_the_day(cli, clock, *, answer_event: bool = True) -> None:
    """Синтетический журнал по таблице событий прогона. «Прервано» на воротах и после остановки не пишется."""
    at(clock, "22:09:00")
    assert cli("new", "demo")[0] == 0

    def step(num: str, start: str, done: str) -> None:
        at(clock, start)
        assert cli("step", "start", num)[0] == 0
        at(clock, done)
        assert cli("step", "done", num)[0] == 0

    at(clock, "22:09:30")
    assert cli("step", "start", "1")[0] == 0
    put_frame(cli)
    at(clock, "22:10:13")
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    at(clock, "22:10:41")  # возобновление сессии с ответом на В1
    assert cli("status", "--brief")[0] == 0
    at(clock, "22:11:11")
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    at(clock, "22:11:12")
    assert cli("step", "done", "1")[0] == 0

    step("2а", "22:11:16", "22:11:43")
    step("2б", "22:11:50", "22:23:24")

    at(clock, "22:23:34")
    assert cli("step", "start", "3")[0] == 0
    at(clock, "22:24:15")
    assert cli("gate", "open", "В3", "--questions", "0")[0] == 0
    at(clock, "22:24:16")
    assert cli("step", "done", "3")[0] == 0

    at(clock, "22:24:38")
    assert cli("step", "start", "4")[0] == 0
    at(clock, "22:27:17")
    assert cli("gate", "open", "В4", "--questions", "1")[0] == 0
    at(clock, "22:27:51")  # возобновление с ответом «2»
    assert cli("status", "--brief")[0] == 0
    at(clock, "22:27:58")
    assert cli("gate", "answer", "В4", "--answer", "2")[0] == 0
    at(clock, "22:28:12")
    assert cli("step", "done", "4")[0] == 0

    step("5", "22:28:18", "22:30:53")

    at(clock, "22:30:55")
    assert cli("step", "start", "6")[0] == 0
    at(clock, "22:36:11")
    assert cli("stop", "--reason", "бюджет израсходован")[0] == 0
    at(clock, "22:36:55")  # возобновление с ответом человека
    assert cli("status", "--brief")[0] == 0
    if answer_event:
        assert cli("budget", "set", "--usd", "9")[0] == 0  # ответ человека исполняет первая команда агента
    at(clock, "22:37:02")
    assert cli("step", "done", "6")[0] == 0

    step("7", "22:37:07", "22:38:05")


def test_run_of_the_day_keeps_the_work_and_writes_no_interrupted(cli, home, clock):
    run_of_the_day(cli, clock)
    root = home / "demo"
    assert not [e for e in journal_of(root) if e.get("status") == "прервано"]
    durations = {
        e["step"]: e["duration_s"]
        for e in journal_of(root)
        if e.get("status") == "выполнено" and e["name"].startswith("шаг ")
    }
    assert durations == {
        "1": 44,
        "2а": 27,
        "2б": 694,
        "3": 42,
        "4": 173,
        "5": 155,
        "6": 323,  # 316 с до остановки и 7 с после ответа человека
        "7": 58,
    }
    # шаг начат по одному разу: повторный `step start` после ответа не нужен
    assert all(len(statuses(root, f"шаг {n}")) == 2 for n in durations)
    assert minutes(cli) == pytest.approx(25.3, abs=0.5)
    assert check_project(root) == []


def test_run_of_the_day_without_a_command_after_the_answer_loses_only_seconds(cli, home, clock):
    """Ответ человека не записан событием: ожидание идёт до `step done`, потеряно 7 секунд работы агента."""
    run_of_the_day(cli, clock, answer_event=False)
    assert minutes(cli) == pytest.approx(25.3, abs=0.5)
    done6 = next(
        e for e in journal_of(home / "demo") if e.get("name") == "шаг 6" and e.get("status") == "выполнено"
    )
    assert done6["duration_s"] == 316


def test_status_shows_the_clock_next_to_the_work_time(cli, home, clock):
    run_of_the_day(cli, clock)
    d = run(cli, "status")
    assert d["clock_minutes"] == pytest.approx((29 * 60 + 5) / 60, abs=0.01)  # от «проекта создан» до шага 7
    assert d["clock_minutes"] > d["work_minutes"]
    code, out, _ = cli("status")
    assert code == 0 and "Время работы: 25,3 из 60 мин" in out
    assert "по часам 29,1 мин (с ожиданием ответов и перерывами)" in out
    brief = run(cli, "status", "--brief")
    assert brief["work_minutes"] == d["work_minutes"] and brief["clock_minutes"] == d["clock_minutes"]
    text = cli("status", "--brief")[1]
    assert "время работы 25,3 мин, по часам 29,1 мин (с ожиданием ответов и перерывами)" in text
    assert len(text.splitlines()) <= 10


# --- Пауза на воротах ---------------------------------------------------------------------------------


def test_resume_at_open_gates_keeps_the_step_started(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    clock.advance(30)
    cli("gate", "open", "В1", "--questions", "2")
    before = journal_of(root)
    clock.advance(600)
    for _ in range(2):  # повторная сводка тоже ничего не пишет
        code, out, _ = cli("status", "--brief")
        assert code == 0
    assert journal_of(root) == before
    assert "на паузе: ждём ответа на В1" in out and "Прервано" not in out
    d = run(cli, "status", "--brief", "--peek")
    assert d["paused"] == "ворота" and d["interrupted"] == [] and d["step_in_progress"] is True
    # ответ на В1, затем шаг закрывается без повторного `step start`
    clock.advance(20)
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    clock.advance(5)
    done = run(cli, "step", "done", "1")
    assert done["duration_s"] == 35  # 30 с до ворот и 5 с после ответа; 620 с ожидания не в счёт
    assert statuses(root, "шаг 1") == ["начато", "выполнено"]
    assert check_project(root) == []


def test_waiting_gate_does_not_count_as_work_while_the_step_is_open(cli, home, clock):
    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    clock.advance(60)
    cli("gate", "open", "В1", "--questions", "1")
    clock.advance(3600)
    assert minutes(cli) == pytest.approx(1.0)  # час ожидания ответа в работу не входит
    assert run(cli, "status")["clock_minutes"] == pytest.approx(1.0)  # по часам — до последнего события


def test_answered_gates_do_not_keep_the_step_on_pause(cli, home, clock):
    """Ответ на воротах записан, а работа так и не продолжилась до перерыва: это настоящий перерыв."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    clock.advance(30)
    cli("gate", "open", "В1", "--questions", "1")
    clock.advance(100)
    cli("gate", "answer", "В1", "--answer", "ок")
    clock.advance(7200)
    d = run(cli, "status", "--brief")
    assert [i["name"] for i in d["interrupted"]] == ["шаг 1"]
    assert statuses(root, "шаг 1") == ["начато", "прервано"]


def test_zero_question_gates_are_not_a_pause(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    cli("gate", "open", "В2", "--questions", "0")
    clock.advance(600)
    d = run(cli, "status", "--brief")
    assert d["paused"] is None and [i["name"] for i in d["interrupted"]] == ["шаг 2а"]
    assert statuses(root, "шаг 2а") == ["начато", "прервано"]


# --- Пауза после «внеплановой остановки» ---------------------------------------------------------------


def test_resume_after_a_stop_keeps_the_step_started_and_says_what_we_wait_for(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(120)
    assert cli("stop", "--reason", "нужен ответ")[0] == 0
    before = journal_of(root)
    clock.advance(900)
    code, out, _ = cli("status", "--brief")
    assert code == 0 and journal_of(root) == before
    assert "на паузе: ждём ответ человека на внеплановую остановку" in out and "Прервано" not in out
    assert run(cli, "status", "--brief", "--peek")["paused"] == "остановка"
    assert "Ждём ответ человека на внеплановую остановку" in cli("status")[1]
    assert minutes(cli) == pytest.approx(2.0)  # ожидание после остановки в работу не входит
    clock.advance(60)
    assert run(cli, "step", "done", "2б")["duration_s"] == 120
    assert statuses(root, "шаг 2б") == ["начато", "выполнено"]


def test_work_after_the_answer_to_a_stop_counts_from_the_next_event(cli, home, clock):
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(120)
    cli("stop", "--reason", "нужен ответ")
    clock.advance(900)
    assert cli("budget", "set", "--usd", "9")[0] == 0  # ответ человека: первое событие после остановки
    clock.advance(45)
    assert run(cli, "step", "done", "2б")["duration_s"] == 165


def test_a_note_about_outside_changes_does_not_end_the_pause(cli, home, clock):
    """«Изменено вне агента» пишет сама сводка: после неё шаг всё так же ждёт ответа, а не «прерывается»."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    cli("stop", "--reason", "нужен ответ")
    claims = root / "claims.yaml"
    claims.write_bytes(claims.read_bytes() + "# правка человека\n".encode())
    clock.advance(60)
    code, out, _ = cli("status", "--brief")
    assert code == 0 and "Изменено вне агента" in out and "на паузе" in out
    assert any(e["kind"] == "изменено вне агента" for e in journal_of(root))
    clock.advance(60)
    out = cli("status", "--brief")[1]
    assert "Прервано" not in out and statuses(root, "шаг 2б") == ["начато"]
    clock.advance(10)
    assert run(cli, "step", "done", "2б")["ok"] is True


def test_ignoring_the_pause_by_starting_a_step_again_records_the_work(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(100)
    cli("stop", "--reason", "нужен ответ")
    clock.advance(500)
    cli("step", "start", "2б")
    interrupted = [e for e in journal_of(root) if e.get("status") == "прервано"]
    assert [e["duration_s"] for e in interrupted] == [100]


# --- Настоящий перерыв -----------------------------------------------------------------------------------


def test_real_break_gets_the_work_up_to_the_last_event_and_no_idle(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(120)
    cli("gate", "open", "В2", "--questions", "0")  # последнее событие перед перерывом
    clock.advance(7200)  # перерыв: сессия закрыта
    d = run(cli, "status", "--brief")
    assert [i["name"] for i in d["interrupted"]] == ["шаг 2а"]
    (event,) = [e for e in journal_of(root) if e.get("status") == "прервано"]
    assert event["duration_s"] == 120
    assert minutes(cli) == pytest.approx(2.0)  # два часа простоя не считаются
    # шаг начат заново и закрыт: складываются обе части работы
    cli("step", "start", "2а")
    clock.advance(30)
    assert cli("step", "done", "2а")[0] == 0
    assert minutes(cli) == pytest.approx(2.5)
    assert check_project(root) == []


def test_real_break_leaves_out_the_gate_waits_before_it(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(60)
    cli("gate", "open", "В2", "--questions", "1")
    clock.advance(40)
    cli("gate", "answer", "В2", "--answer", "ок")  # конец ожидания и последнее событие перед перерывом
    clock.advance(7200)
    cli("status", "--brief")
    (event,) = [e for e in journal_of(root) if e.get("status") == "прервано"]
    assert event["duration_s"] == 60  # 100 с от начала шага минус 40 с ожидания


def test_waits_that_overlap_are_taken_off_once(cli, home, clock):
    """Остановка посреди ожидания на воротах: общий отрезок вычитается один раз."""
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(60)
    cli("gate", "open", "В2", "--questions", "1")
    clock.advance(30)
    cli("stop", "--reason", "уточнение")
    clock.advance(30)
    cli("gate", "answer", "В2", "--answer", "ок")
    clock.advance(10)
    assert run(cli, "step", "done", "2а")["duration_s"] == 70  # 60 с до ворот и 10 с после ответа


def test_restart_without_done_writes_the_work_of_the_lost_attempt(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "1")
    clock.advance(75)
    cli("gate", "open", "В2", "--questions", "0")  # событие проекта: работа дошла до сюда
    clock.advance(500)
    cli("step", "start", "1")
    (event,) = [e for e in journal_of(root) if e.get("status") == "прервано"]
    assert event["duration_s"] == 75


def test_old_journal_without_duration_on_interrupted_reads_as_before(cli, home, clock):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(120)
    cli("gate", "open", "В2", "--questions", "0")
    clock.advance(600)
    cli("status", "--brief")
    cli("step", "start", "2а")
    clock.advance(30)
    cli("step", "done", "2а")
    assert minutes(cli) == pytest.approx(2.5)

    def old_style(events):
        for e in events:
            if e.get("status") == "прервано":
                e.pop("duration_s")

    edit_journal(root, old_style)
    rehash(root)
    assert all("duration_s" not in e for e in journal_of(root) if e.get("status") == "прервано")
    assert check_project(root) == []
    before = tree_hashes(root)
    assert minutes(cli) == pytest.approx(
        0.5
    )  # у старой записи длительности нет: считается только выполненное
    assert cli("status", "--brief", "--peek")[0] == 0
    assert tree_hashes(root) == before


def test_interrupted_duration_is_optional_in_the_model():
    from strateg.models import JournalEvent

    base = {
        "schema_version": 1,
        "time": "2026-11-08T22:00:00+03:00",
        "actor": "CLI",
        "kind": "действие",
        "step": "1",
        "name": "шаг 1",
        "idempotency_hash": "a" * 64,
        "status": "прервано",
    }
    assert JournalEvent.model_validate(base).duration_s is None
    assert JournalEvent.model_validate({**base, "duration_s": 12.5}).duration_s == 12.5
    with pytest.raises(ValueError):
        JournalEvent.model_validate({**base, "status": "выполнено"})  # у «выполнено» длительность обязательна


# --- Чистые функции времени ---------------------------------------------------------------------------


def test_waited_seconds_merges_overlapping_waits():
    t0 = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=ZONE)

    def sec(n):
        return t0 + dt.timedelta(seconds=n)

    waits = [(sec(10), sec(50)), (sec(30), sec(70)), (sec(100), None)]
    assert proj.waited_seconds(sec(0), sec(120), waits) == 60 + 20
    assert proj.waited_seconds(sec(60), sec(110), waits) == 10 + 10
    assert proj.waited_seconds(sec(0), sec(5), waits) == 0


# --- Ход проекта: события хуков и «прервано» от сводки паузу не меняют -----------------------------------


@pytest.mark.parametrize("kind", list(HOOK_EVENTS))
def test_a_hook_event_after_a_stop_does_not_end_the_wait(cli, home, clock, kind):
    """Остановка → событие хука → ответ человека → `step done`: событие хука ожидание не обрывает."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(120)
    assert cli("stop", "--reason", "нужен ответ")[0] == 0
    clock.advance(1)
    hook(root, kind)
    clock.advance(900)
    d = run(cli, "status")
    assert d["paused"] == "остановка" and d["step_in_progress"] is True  # пауза, бюджет времени не тикает
    assert minutes(cli) == pytest.approx(2.0)
    assert cli("budget", "set", "--usd", "9")[0] == 0  # первая команда агента после ответа человека
    clock.advance(30)
    assert run(cli, "step", "done", "2б")["duration_s"] == 150  # 120 до остановки и 30 после ответа
    assert statuses(root, "шаг 2б") == ["начато", "выполнено"]
    assert check_project(root) == []


@pytest.mark.parametrize("kind", ["роль выполнена", "учёт недоступен"])
def test_a_hook_event_after_a_stop_does_not_hide_the_pause_from_the_summary(cli, home, clock, kind):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(120)
    cli("stop", "--reason", "нужен ответ")
    clock.advance(1)
    hook(root, kind)
    clock.advance(900)
    before = journal_of(root)
    code, out, _ = cli("status", "--brief")
    assert code == 0 and "на паузе: ждём ответ человека на внеплановую остановку" in out
    assert "Прервано" not in out and journal_of(root) == before
    assert run(cli, "status", "--brief", "--peek")["paused"] == "остановка"


def test_a_researcher_task_after_a_stop_ends_the_wait(cli, home, clock):
    """«Задание исследователю» пишет хук, но это работа агента: после остановки она заканчивает ожидание."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2б")
    clock.advance(120)
    cli("stop", "--reason", "нужен ответ")
    clock.advance(900)
    assert effects.append_event(root, EventSpec(proj.RESEARCHER_TASK, "найди тарифы", role="researcher"))
    assert run(cli, "status", "--brief", "--peek")["paused"] is None  # работа идёт дальше
    clock.advance(30)
    assert run(cli, "step", "done", "2б")["duration_s"] == 150
    assert check_project(root) == []


def test_the_researcher_task_name_is_the_one_the_hook_writes():
    from pathlib import Path

    rules = (Path(effects.__file__).parent / "rules.py").read_text(encoding="utf-8")
    assert f'EventSpec("{proj.RESEARCHER_TASK}"' in rules


@pytest.mark.parametrize("kind", list(HOOK_EVENTS))
def test_a_hook_event_after_gates_open_keeps_the_pause(cli, home, clock, kind):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    clock.advance(2)
    hook(root, kind)
    clock.advance(600)
    before = journal_of(root)
    for _ in range(2):  # и повторная сводка ничего не пишет
        code, out, _ = cli("status", "--brief")
        assert code == 0 and "на паузе: ждём ответа на В1" in out and "Прервано" not in out
    d = run(cli, "status", "--brief")
    assert d["paused"] == "ворота" and d["interrupted"] == []
    assert statuses(root, "шаг 1") == ["начато"]
    assert journal_of(root) == before
    clock.advance(20)
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    clock.advance(5)
    assert (
        run(cli, "step", "done", "1")["duration_s"] == 5
    )  # ворота открыты сразу после рамки; ожидание не в счёт
    assert check_project(root) == []


def test_the_note_of_the_summary_does_not_end_the_pause(cli, home, clock):
    """Начатое `strateg fetch` сводка прерывает; вторая сводка шаг на воротах не прерывает."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "1")
    put_frame(cli)
    p = proj.Project.load(root)
    with proj.transaction(root):
        p.add_event(
            p.event(
                kind="действие",
                name="strateg fetch",
                idempotency_hash=proj.key_hash("fetch", "https://example.com/a"),
                status="начато",
            )
        )
    clock.advance(10)
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    clock.advance(60)
    first = run(cli, "status", "--brief")
    assert [i["name"] for i in first["interrupted"]] == ["strateg fetch"] and first["paused"] == "ворота"
    assert statuses(root, "шаг 1") == ["начато"]
    clock.advance(60)
    second = run(cli, "status", "--brief")
    assert second["interrupted"] == [] and second["paused"] == "ворота"
    assert statuses(root, "шаг 1") == ["начато"]
    assert [e["status"] for e in journal_of(root) if e.get("name") == "strateg fetch"] == [
        "начато",
        "прервано",
    ]
    assert check_project(root) == []


def test_the_note_of_the_summary_does_not_move_the_clock(cli, home, clock):
    """«Прервано» от сводки — не ход проекта: часы и работа шага кончаются на последнем настоящем событии."""
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(120)
    cli("gate", "open", "В2", "--questions", "0")
    clock.advance(7200)
    cli("status", "--brief")  # сводка пишет «прервано» шагу
    assert run(cli, "status")["clock_minutes"] == pytest.approx(2.0)  # не 122
    (event,) = [e for e in journal_of(root) if e.get("status") == "прервано"]
    assert event["duration_s"] == 120


def test_progress_events_leave_out_hook_events_but_keep_the_researcher_task_and_steps_own_notes(
    cli, home, clock
):
    root = home / "demo"
    cli("new", "demo")
    cli("step", "start", "2а")
    clock.advance(5)
    hook(root, "учёт недоступен")
    assert effects.append_event(root, EventSpec(proj.RESEARCHER_TASK, "найди", role="researcher"))
    clock.advance(5)
    cli("step", "start", "2а")  # «прервано» от самого `step start` — действие агента, ход проекта
    p = proj.Project.load(root)
    progress = proj.progress_events(p.journal)
    assert not [e for e in progress if e.kind == "учёт недоступен"]
    assert [e for e in progress if e.kind == proj.RESEARCHER_TASK]
    assert sum(1 for e in progress if e.status == "прервано") == 1
