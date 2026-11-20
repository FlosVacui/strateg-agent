"""Хуки `Stop`, `SubagentStop` и бюджет в `PreToolUse` (ADR-0010, ADR-0009): стоимость и отказ."""

from __future__ import annotations

import json

import pytest

from helpers import journal_of
from strateg import project as proj
from strateg.hooks import effects, runner
from strateg.project_check import check_project
from test_hooks_runner import env, kinds, pre
from transcripts import OPUS, SONNET, agent_file, append, assistant, session_files, write

SESSION = "sess-1"


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


@pytest.fixture
def demo(cli, home):
    assert cli("new", "demo")[0] == 0
    return home / "demo"


@pytest.fixture
def tx(tmp_path):
    """Основной транскрипт сессии и папка транскриптов субагентов."""
    main, folder = session_files(tmp_path / "claude", SESSION)
    write(main)  # пустой, но есть
    return main, folder


def costs(root) -> list[dict]:
    text = (root / "cost.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def stop_event(main, **extra) -> bytes:
    data = {
        "hook_event_name": "Stop",
        "session_id": SESSION,
        "transcript_path": str(main),
        "cwd": str(main.parent),
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def subagent_event(main, agent_id, agent_type, path, **extra) -> bytes:
    data = {
        "hook_event_name": "SubagentStop",
        "session_id": SESSION,
        "transcript_path": str(main),
        "cwd": str(main.parent),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "agent_transcript_path": str(path),
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def run_stop(main, e=None, **extra):
    return runner.run_stop(stop_event(main, **extra), e or env())


def assert_clean(root):
    assert check_project(root) == []
    assert proj.Project.load(root).outside == []


def down_events(root) -> list[dict]:
    return [e for e in journal_of(root) if e["kind"] == "учёт недоступен"]


# --- Stop -----------------------------------------------------------------------------------------------


def test_stop_writes_the_cost_of_the_main_transcript_with_role_and_step(demo, tx):
    main, _ = tx
    write(main, assistant("m1", SONNET, out=1000), assistant("m2", OPUS, inp=100, cache_read=5000))
    result = run_stop(main)
    assert result.stdout == "" and result.exit_code == 0  # хук ничего не печатает
    one, two = costs(demo)
    assert (one["role"], one["step"], one["session_id"], one["message_id"]) == (
        "основной",
        "1",
        SESSION,
        "m1",
    )
    assert one["cost_source"] == "оценка" and one["cost_usd"] == pytest.approx(0.01)
    assert two["model"] == OPUS and two["cost_usd"] == pytest.approx((100 * 4 + 5000 * 0.2) / 1e6)
    assert_clean(demo)


def test_stop_on_every_turn_does_not_double_and_takes_only_new_messages(demo, tx):
    main, _ = tx
    write(main, assistant("m1", out=10))
    run_stop(main)
    run_stop(main)
    append(main, assistant("m2", out=20), assistant("m2", out=20))
    run_stop(main)
    run_stop(main)
    assert [r["message_id"] for r in costs(demo)] == ["m1", "m2"]
    assert_clean(demo)


def test_stop_without_an_active_project_does_nothing(home, tx):
    main, _ = tx
    write(main, assistant("m1", out=10))
    assert run_stop(main).stdout == "" and not home.exists()


def test_stop_does_nothing_when_hooks_are_off_or_the_environment_is_foreign(demo, tx):
    main, _ = tx
    write(main, assistant("m1", out=10))
    run_stop(main, env(STRATEG_HOOKS="0"))
    foreign = {
        "hook_event_name": "stop",
        "conversation_id": "c1",
        "generation_id": "g",
        "session_id": SESSION,
    }
    foreign["transcript_path"] = str(main)
    runner.run_stop(json.dumps(foreign).encode("utf-8"), env())
    assert costs(demo) == []


def test_stop_on_garbage_input_does_not_raise(demo):
    for raw in (b"", b"not json", b"[]", b'{"hook_event_name": "Stop"}'):
        assert runner.run_stop(raw, env()).stdout == ""
        assert runner.run_subagent_stop(raw, env()).stdout == ""


# --- SubagentStop ---------------------------------------------------------------------------------------


def test_subagent_stop_writes_the_subagent_cost_with_its_role_and_a_trace_of_the_role(demo, tx):
    main, folder = tx
    path = agent_file(folder, "a1", assistant("s1", SONNET, out=500), assistant("s2", SONNET, out=500))
    result = runner.run_subagent_stop(subagent_event(main, "a1", "researcher", path), env())
    assert result.stdout == ""
    assert [(r["role"], r["message_id"]) for r in costs(demo)] == [("researcher", "s1"), ("researcher", "s2")]
    (done,) = [e for e in journal_of(demo) if e["kind"] == "роль выполнена"]
    assert done["role"] == "researcher" and done["actor"] == "хук" and done["name"] == "a1"
    assert "роль researcher" in done["message"] and done["step"] == "1"
    assert_clean(demo)


def test_role_trace_is_written_once_per_subagent(demo, tx):
    main, folder = tx
    path = agent_file(folder, "a1", assistant("s1", out=5))
    for _ in range(3):
        runner.run_subagent_stop(subagent_event(main, "a1", "critic", path), env())
    assert kinds(demo).count("роль выполнена") == 1
    path2 = agent_file(folder, "a2", assistant("s9", out=5))
    runner.run_subagent_stop(subagent_event(main, "a2", "critic", path2), env())
    assert kinds(demo).count("роль выполнена") == 2
    assert_clean(demo)


def test_internal_agent_without_a_type_is_counted_but_leaves_no_role_trace(demo, tx):
    main, folder = tx
    path = agent_file(folder, "p1", assistant("s1", out=5))
    runner.run_subagent_stop(subagent_event(main, "p1", "", path), env())
    assert [r["role"] for r in costs(demo)] == ["служебный"]
    assert "роль выполнена" not in kinds(demo)


def test_subagent_is_counted_once_even_when_both_stop_hooks_see_it(demo, tx):
    """Транскрипт субагента лежит отдельно от основного; Stop видит его в папке и не считает второй раз."""
    main, folder = tx
    write(main, assistant("m1", out=100))
    path = agent_file(folder, "a1", assistant("s1", out=200), agent_type="researcher")
    runner.run_subagent_stop(subagent_event(main, "a1", "researcher", path), env())
    run_stop(main)
    run_stop(main)
    got = sorted((r["role"], r["message_id"]) for r in costs(demo))
    assert got == [("researcher", "s1"), ("основной", "m1")]
    assert_clean(demo)


def test_stop_counts_a_subagent_whose_stop_hook_was_lost_under_the_role_from_its_meta(demo, tx):
    main, folder = tx
    agent_file(folder, "a1", assistant("s1", out=200), agent_type="critic")
    agent_file(folder, "a2", assistant("s2", out=200))  # роль неизвестна: чужую роль не угадываем
    run_stop(main)
    assert [(r["role"], r["message_id"]) for r in costs(demo)] == [("critic", "s1")]


def test_two_sessions_and_three_roles_land_in_one_project(demo, tx, tmp_path):
    main, folder = tx
    write(main, assistant("m1", out=100))
    runner.run_subagent_stop(
        subagent_event(main, "a1", "researcher", agent_file(folder, "a1", assistant("s1", out=100))), env()
    )
    runner.run_subagent_stop(
        subagent_event(main, "a2", "critic", agent_file(folder, "a2", assistant("s2", out=100))), env()
    )
    run_stop(main)
    main2, _ = session_files(tmp_path / "claude", "sess-2")
    write(main2, assistant("n1", out=100))
    runner.run_stop(stop_event(main2, session_id="sess-2"), env())
    got = {(r["session_id"], r["role"]) for r in costs(demo)}
    assert got == {
        (SESSION, "основной"),
        (SESSION, "researcher"),
        (SESSION, "critic"),
        ("sess-2", "основной"),
    }
    assert_clean(demo)


# --- сбои учёта -----------------------------------------------------------------------------------------


def test_accounting_failure_writes_one_event_per_session_and_does_not_block(demo, tmp_path):
    gone = tmp_path / "claude" / "missing.jsonl"
    first = run_stop(gone)
    second = run_stop(gone)
    assert first.stdout == "" and second.stdout == "" and first.exit_code == 0
    events = down_events(demo)
    assert len(events) == 1 and events[0]["name"] == SESSION and events[0]["actor"] == "хук"
    assert "FileNotFoundError" in events[0]["message"]
    other = runner.run_stop(stop_event(gone, session_id="sess-2"), env())
    assert other.stdout == "" and len(down_events(demo)) == 2  # новая сессия — новое событие
    assert_clean(demo)


def test_event_without_a_transcript_path_is_an_accounting_failure_too(demo):
    raw = json.dumps({"hook_event_name": "Stop", "session_id": SESSION}).encode("utf-8")
    assert runner.run_stop(raw, env()).stdout == ""
    assert len(down_events(demo)) == 1


def test_unreadable_price_table_is_a_failure_not_a_crash(demo, tx, tmp_path, monkeypatch):
    main, _ = tx
    write(main, assistant("m1", out=10))
    bad = tmp_path / "cfg"
    bad.mkdir()
    (bad / "prices.yaml").write_text("models: 5", encoding="utf-8")
    monkeypatch.setenv("STRATEG_CONFIG_DIR", str(bad))
    run_stop(main)
    assert costs(demo) == [] and len(down_events(demo)) == 1


def test_model_without_a_price_is_noted_once_per_model(demo, tx):
    main, _ = tx
    write(main, assistant("m1", "claude-future-9", out=10), assistant("m2", "claude-future-9", out=10))
    run_stop(main)
    append(main, assistant("m3", "claude-future-9", out=10), assistant("m4", "claude-other-1", out=10))
    run_stop(main)
    names = sorted(e["name"] for e in down_events(demo))
    assert names == ["нет цены: claude-future-9", "нет цены: claude-other-1"]
    assert all(r["cost_source"] == "нет данных" for r in costs(demo))
    assert_clean(demo)


# --- бюджет в PreToolUse --------------------------------------------------------------------------------


def pre_tx(main, tool, tin=None, agent=None, e=None, **extra):
    return pre(tool, tin, agent, e, transcript_path=str(main), **extra)


@pytest.fixture
def small(cli, demo):
    """Бюджет $1: 100 000 токенов вывода Sonnet — ровно $1."""
    assert cli("budget", "set", "--usd", "1")[0] == 0
    return demo


def spend(main, out_tokens, mid="m1"):
    write(main, assistant(mid, SONNET, out=out_tokens))


def test_at_79_percent_nothing_is_refused(small, tx):
    main, _ = tx
    spend(main, 79_000)
    assert pre_tx(main, "WebSearch", {"query": "цена"}, "researcher") is None
    body = pre_tx(main, "Agent", {"subagent_type": "critic", "prompt": "проверь"})
    assert body["permissionDecision"] == "allow"
    assert pre_tx(main, "Bash", {"command": "ls"}) is None


def test_at_100_percent_paid_calls_are_refused_in_the_middle_of_a_turn(small, tx, cli):
    """Stop не звали: платный вызов сам дочитывает транскрипт и видит исчерпание."""
    main, _ = tx
    spend(main, 100_000)
    assert costs(small) == []
    assert cli("ux", "add", "https://example.com")[0] == 0  # переход по списку доходит до бюджета
    for tool, tin, agent in (
        ("WebSearch", {"query": "цена"}, "researcher"),
        ("Agent", {"subagent_type": "critic", "prompt": "проверь"}, None),
        ("Agent", {"subagent_type": "researcher", "prompt": "Вопрос: цена"}, None),
        ("mcp__playwright__browser_navigate", {"url": "https://example.com"}, "researcher"),
        ("Bash", {"command": "uv run strateg fetch https://example.com"}, "researcher"),
    ):
        body = pre_tx(main, tool, tin, agent)
        assert body is not None and body["permissionDecision"] == "deny", tool
        assert "Бюджет исчерпан" in body["permissionDecisionReason"], tool
    assert len(costs(small)) == 1  # расход записан тем же вызовом


def test_refusal_names_what_ran_out_and_the_three_answers(small, tx):
    main, _ = tx
    spend(main, 100_000)
    text = pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecisionReason"]
    assert "кончилось: деньги (" in text and "$1,00 из $1,00" in text
    assert "(1) добавить бюджет" in text and "strateg budget set" in text
    assert "(2) выпустить" in text and "пометкой о неполноте" in text and "--action выпуск" in text
    assert "(3) остановиться" in text


def test_refusal_names_time_when_the_time_ran_out(cli, small, tx, clock):
    main, _ = tx
    spend(main, 1000)
    assert cli("step", "start", "2а")[0] == 0
    clock.advance(61 * 60)
    text = pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecisionReason"]
    assert "кончилось: время" in text and "61,0 из 60 мин" in text


def test_both_money_and_time_are_named_when_both_ran_out(cli, small, tx, clock):
    main, _ = tx
    spend(main, 100_000)
    assert cli("step", "start", "2а")[0] == 0
    clock.advance(61 * 60)
    text = pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecisionReason"]
    assert "кончилось: деньги и время" in text


def test_what_stays_open_when_the_budget_is_exhausted(small, tx, home):
    main, _ = tx
    spend(main, 100_000)
    outputs = str(home / "demo" / "outputs" / "report.md")
    assert pre_tx(main, "Bash", {"command": "uv run strateg status"}) is None
    assert pre_tx(main, "Bash", {"command": "uv run strateg budget set --usd 0.5"}) is None  # лимит не растёт
    assert pre_tx(main, "Bash", {"command": "uv run strateg budget check --action выпуск"}) is None
    assert pre_tx(main, "Bash", {"command": "uv run strateg cost report --json"}) is None
    assert pre_tx(main, "Read", {"file_path": str(home / "demo" / "journal.jsonl")}) is None
    assert pre_tx(main, "Grep", {"pattern": "x", "path": str(home / "demo" / "outputs")}) is None
    assert pre_tx(main, "Write", {"file_path": outputs, "content": "Выпуск с пометкой о неполноте"}) is None
    assert pre_tx(main, "Edit", {"file_path": outputs, "old_string": "a", "new_string": "b"}) is None


@pytest.mark.parametrize(
    ("tool", "tin"),
    [
        ("Bash", {"command": "ls"}),
        ("Bash", {"command": "uv run strateg status; ls"}),
        ("Bash", {"command": "uv run strateg status | head"}),
        ("Bash", {"command": "echo 1"}),
        ("PowerShell", {"command": "Get-ChildItem"}),
        ("Monitor", {"command": "tail -f x"}),
    ],
)
def test_other_commands_are_refused_when_the_budget_is_exhausted(small, tx, tool, tin):
    main, _ = tx
    spend(main, 100_000)
    body = pre_tx(main, tool, tin)
    assert body["permissionDecision"] == "deny" and "Бюджет исчерпан" in body["permissionDecisionReason"]


def test_writes_outside_outputs_and_reads_outside_the_project_are_refused_when_exhausted(small, tx, home):
    main, _ = tx
    spend(main, 100_000)
    demo = home / "demo"
    for tool, tin in (
        ("Write", {"file_path": str(demo / ".tmp" / "draft.yaml"), "content": "x"}),
        ("Write", {"file_path": str(demo / "inputs" / "a.txt"), "content": "x"}),
        ("Read", {"file_path": str(home.parent / "elsewhere.txt")}),
        ("Grep", {"pattern": "x"}),  # без path: поиск от корня продукта, не в папке проекта
    ):
        body = pre_tx(main, tool, tin)
        assert body is not None and body["permissionDecision"] == "deny", tool


def test_permissions_come_first_then_the_budget(small, tx):
    main, _ = tx
    spend(main, 100_000)
    body = pre_tx(main, "WebFetch", {"url": "https://example.com", "prompt": "x"})
    assert "WebFetch закрыт" in body["permissionDecisionReason"]  # текст прав, не бюджета
    body = pre_tx(main, "WebSearch", {"query": "x"})  # основному агенту веб закрыт правами
    assert "Веб-поиск закрыт" in body["permissionDecisionReason"]
    assert costs(small) == []  # отказ по правам расход не дочитывает


def test_a_refusal_by_budget_is_written_once_per_tool_and_state(small, tx):
    main, _ = tx
    spend(main, 100_000)
    for _ in range(3):
        pre_tx(main, "WebSearch", {"query": "x"}, "researcher")
    pre_tx(main, "Bash", {"command": "ls"})
    events = [e for e in journal_of(small) if e["kind"] == "бюджет" and e["actor"] == "хук"]
    assert len(events) == 2
    assert "бюджет исчерпан (деньги)" in events[0]["message"] and "WebSearch" in events[0]["message"]
    assert events[0]["role"] == "researcher"
    assert_clean(small)


def test_adding_budget_opens_the_calls_again(cli, small, tx):
    main, _ = tx
    spend(main, 100_000)
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecision"] == "deny"
    assert cli("budget", "set", "--usd", "5")[0] == 0
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher") is None
    assert pre_tx(main, "Bash", {"command": "ls"}) is None


def test_release_reserve_is_in_the_cli_and_the_hook_keeps_the_release_path_open(cli, small, tx):
    """$1,20 при лимите $1 и резерве $0,50: CLI даёт выпуск, хук закрывает платное и оставляет `outputs/`."""
    main, _ = tx
    spend(main, 120_000)
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecision"] == "deny"
    assert cli("budget", "check")[0] == 1
    assert cli("budget", "check", "--action", "выпуск")[0] == 0
    assert pre_tx(main, "Bash", {"command": "uv run strateg budget check --action выпуск"}) is None


def test_paid_call_that_is_allowed_still_records_the_cost_for_the_next_check(small, tx):
    main, _ = tx
    spend(main, 50_000)
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher") is None
    assert [r["message_id"] for r in costs(small)] == ["m1"]
    append(main, assistant("m2", SONNET, out=50_000))
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecision"] == "deny"


def test_calls_that_stay_open_do_not_read_the_transcript(small, tx, home):
    """Команда `strateg` и чтение в проекте бюджет не закрывает: на них хук не тратит время на транскрипт."""
    main, _ = tx
    spend(main, 10_000)
    assert pre_tx(main, "Bash", {"command": "uv run strateg status"}) is None
    assert pre_tx(main, "Read", {"file_path": str(home / "demo" / "journal.jsonl")}) is None
    assert costs(small) == []
    assert pre_tx(main, "Bash", {"command": "ls"}) is None  # такой вызов исчерпание закрыло бы: расход свежий
    assert [r["message_id"] for r in costs(small)] == ["m1"]


def test_subagent_cost_counts_for_the_budget_in_the_middle_of_its_work(small, tx):
    main, folder = tx
    agent_file(folder, "a1", assistant("s1", SONNET, out=100_000), agent_type="researcher")
    body = pre_tx(main, "Agent", {"subagent_type": "critic", "prompt": "x"})
    assert body["permissionDecision"] == "deny" and costs(small)[0]["role"] == "researcher"


def test_accounting_failure_lets_the_call_through_and_writes_one_event(small, tx):
    main, _ = tx
    gone = main.with_name("gone.jsonl")
    for _ in range(2):
        body = pre_tx(gone, "WebSearch", {"query": "x"}, "researcher")
        assert body is None
    assert len(down_events(small)) == 1 and "FileNotFoundError" in down_events(small)[0]["message"]
    assert_clean(small)


def test_unreadable_project_lets_the_call_through_with_an_event(small, tx):
    main, _ = tx
    (small / "claims.yaml").write_text("claims: [", encoding="utf-8")  # проект не читается
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert len(down_events(small)) == 1


def test_no_active_project_means_no_budget(cli, home, tx):
    main, _ = tx
    spend(main, 100_000)
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher") is None


def test_foreign_environment_has_no_budget_from_this_hook(small, tx):
    main, _ = tx
    spend(main, 100_000)
    foreign = {
        "hook_event_name": "preToolUse",
        "conversation_id": "c1",
        "generation_id": "g1",
        "tool_name": "Shell",
        "tool_input": {"command": "ls"},
        "transcript_path": str(main),
    }
    out = runner.run_pre_tool_use(json.dumps(foreign).encode("utf-8"), env()).stdout
    assert "Бюджет" not in out


def test_hooks_off_switches_the_budget_off_too(small, tx):
    main, _ = tx
    spend(main, 100_000)
    out = runner.run_pre_tool_use(
        json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "transcript_path": str(main),
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        ).encode("utf-8"),
        env(STRATEG_HOOKS="0"),
    ).stdout
    assert out == ""


# --- разбор команд для бюджета --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "only", "fetch"),
    [
        ("uv run strateg status", True, False),
        ("uv run --frozen strateg cost report --json", True, False),
        ("uv run strateg budget check --action выпуск", True, False),
        ("python -m strateg.cli status", True, False),
        ("uv run strateg status && uv run strateg check", True, False),
        ("uv run strateg fetch https://example.com", True, True),
        ("uv run strateg status; uv run strateg fetch 'https://example.com/a b'", True, True),
        ("uv run strateg status; ls", False, False),
        ("uv run strateg status | head", False, False),
        ("uv run strateg status > out.txt", False, False),
        ("uv run strateg add $(ls)", False, False),
        ("ls; uv run strateg fetch https://example.com", False, True),
        ("echo uv run strateg status", False, False),
        ("curl https://example.com", False, False),
        ("", False, False),
        ("uv run strateg status 'нет закрывающей кавычки", False, False),
    ],
)
def test_strateg_calls_tell_free_commands_from_fetch_and_foreign_ones(command, only, fetch):
    from strateg.hooks import shell

    got = shell.strateg_calls(command)
    assert (got.only, got.fetch) == (only, fetch), command


def test_paid_calls_are_agents_web_search_playwright_and_fetch():
    from strateg.hooks import cost

    assert cost.is_paid("Agent", {}) and cost.is_paid("Task", {}) and cost.is_paid("WebSearch", {})
    assert cost.is_paid("mcp__playwright__browser_navigate", {})
    assert cost.is_paid("mcp__plugin_playwright_playwright__browser_click", {})
    assert cost.is_paid("Bash", {"command": "uv run strateg fetch https://example.com"})
    assert cost.is_paid("PowerShell", {"command": "uv run strateg fetch https://example.com"})
    for tool, tin in (
        ("Bash", {"command": "uv run strateg status"}),
        ("Bash", {"command": "ls"}),
        ("Bash", {}),
        ("Read", {"file_path": "x"}),
        ("Write", {"file_path": "x"}),
        ("Skill", {}),
    ):
        assert not cost.is_paid(tool, tin), tool


# --- срок бюджетной части, сообщение «учёт недоступен», шаг, повышение лимита, частота пересчёта ----------


@pytest.fixture
def wall(monkeypatch):
    """Часы пересчёта (секунды Unix) под управлением теста."""
    from strateg import offsets

    now = [1_000_000.0]
    monkeypatch.setattr(offsets, "wall", lambda: now[0])
    return now


def test_gate_hands_what_is_left_of_one_deadline_to_every_wait(small, tx, monkeypatch):
    """Единый срок на всю бюджетную часть: пересчёт и чтение проекта делят остаток, а не берут по 5 с."""
    import time

    from strateg import accounting
    from strateg.hooks import cost

    main, _ = tx
    monkeypatch.delenv("STRATEG_LOCK_TIMEOUT", raising=False)
    seen = {}

    def slow_account(root, session, sources, *, prices=None, timeout=None):
        seen["account"] = timeout
        time.sleep(0.3)
        return accounting.Result()

    real_load = proj.Project.load

    def load(root, timeout=None):
        seen["load"] = timeout
        return real_load(root, timeout=timeout)

    monkeypatch.setattr(accounting, "account", slow_account)
    monkeypatch.setattr(proj.Project, "load", staticmethod(load))
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert seen["account"] <= cost.GATE_SECONDS
    assert seen["load"] <= seen["account"] - 0.25


def test_busy_project_in_the_recount_lets_the_call_through_with_one_short_event(small, tx, monkeypatch):
    from strateg import accounting, storage
    from strateg.hooks import cost

    main, _ = tx
    notes = []
    real = effects.append_event

    def spy(root, spec, timeout=None):
        if spec.kind == "учёт недоступен":
            notes.append(timeout)
        return real(root, spec, timeout)

    def busy(root, session, sources, *, prices=None, timeout=None):
        raise storage.LockTimeout(root, 5)

    def busy_load(root, timeout=None):
        raise storage.LockTimeout(root, 5)

    monkeypatch.setattr(accounting, "account", busy)
    monkeypatch.setattr(
        proj.Project, "load", staticmethod(busy_load)
    )  # папка занята и дальше: событие всё равно одно
    monkeypatch.setattr(effects, "append_event", spy)
    for _ in range(2):
        assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher") is None
    assert notes == [cost.NOTE_SECONDS, cost.NOTE_SECONDS]  # по одной записи на вызов, не дольше 0,5 с
    assert len(down_events(small)) == 1 and "LockTimeout" in down_events(small)[0]["message"]


def test_busy_project_on_reading_it_writes_one_short_event_not_two(small, tx, monkeypatch):
    from strateg import storage
    from strateg.hooks import cost

    main, _ = tx
    notes = []
    real = effects.append_event

    def spy(root, spec, timeout=None):
        notes.append((spec.kind, timeout))
        return real(root, spec, timeout)

    def busy(root, timeout=None):
        raise storage.LockTimeout(root, 5)

    monkeypatch.setattr(proj.Project, "load", staticmethod(busy))
    monkeypatch.setattr(effects, "append_event", spy)
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert notes == [("учёт недоступен", cost.NOTE_SECONDS)]


def test_accounting_down_event_has_the_kind_of_error_but_no_path_and_no_text(demo, tx, monkeypatch):
    """У `OSError` в тексте полный путь: в журнале (он уходит в архив) ему не место."""
    from strateg import accounting

    main, _ = tx
    write(main, assistant("m1", out=10))
    (demo / "cost.jsonl").unlink()  # настоящий сбой: файла учёта нет
    run_stop(main)
    secret = chr(92).join(["C:", "Users", "Иван", "AppData", "cost.jsonl"])

    def boom(*args, **kwargs):
        raise PermissionError(13, "Отказано в доступе", secret)

    monkeypatch.setattr(accounting, "account", boom)
    runner.run_stop(stop_event(main, session_id="sess-2"), env())
    pre_tx(main, "WebSearch", {"query": "x"}, "researcher", session_id="sess-3")
    events = down_events(demo)
    assert len(events) == 3
    assert "FileNotFoundError" in events[0]["message"] and "PermissionError" in events[1]["message"]
    for e in events:
        for leak in (str(demo), str(demo.parent), "cost.jsonl", "Иван", "Users", "Отказано", chr(92), "/"):
            assert leak not in e["message"], (leak, e["message"])


def test_budget_events_hold_numbers_and_the_tool_but_no_paths(small, tx, home):
    main, _ = tx
    spend(main, 100_000)
    pre_tx(main, "WebSearch", {"query": "x"}, "researcher")
    (event,) = [e for e in journal_of(small) if e["kind"] == "бюджет" and e["actor"] == "хук"]
    assert str(home) not in event["message"] and chr(92) not in event["message"]


def test_cost_before_a_step_command_stays_on_the_old_step(small, tx, cli):
    """`strateg step …` не платная и открыта, но расход до неё принадлежит прежнему шагу."""
    main, _ = tx
    write(main, *[assistant(f"a{i}", out=10) for i in range(5)])
    assert pre_tx(main, "Bash", {"command": "uv run strateg step start 2а"}) is None
    assert cli("step", "start", "2а")[0] == 0
    append(main, *[assistant(f"b{i}", out=10) for i in range(3)])
    run_stop(main)
    got = [(r["step"], r["message_id"][0]) for r in costs(small)]
    assert got == [("1", "a")] * 5 + [("2а", "b")] * 3
    assert_clean(small)


def test_a_status_command_does_not_recount(small, tx):
    main, _ = tx
    spend(main, 10)
    assert pre_tx(main, "Bash", {"command": "uv run strateg status"}) is None
    assert costs(small) == []


EXHAUSTED_SETS = [
    "uv run strateg budget set --usd 100",
    "uv run strateg budget set --usd=100",
    "uv run strateg budget set --us 100",
    "uv run strateg budget set --minutes 999",
    "uv run strateg budget set --usd 0.5 --minutes 999",
    "uv run strateg budget set --usd 100 --json",
    "uv run strateg budget set --usd много",
    "uv run strateg budget set --usd 100 --project demo",
    "uv run strateg status && uv run strateg budget set --usd 100",
    "python -m strateg.cli budget set --usd 100",
]


@pytest.mark.parametrize("command", EXHAUSTED_SETS)
def test_raising_the_limit_when_exhausted_asks_the_human(small, tx, command):
    main, _ = tx
    spend(main, 100_000)
    body = pre_tx(main, "Bash", {"command": command})
    assert body["permissionDecision"] == "ask", command
    text = body["permissionDecisionReason"]
    assert "Бюджет исчерпан" in text and "человек" in text and "--action выпуск" in text


def test_asking_about_a_raise_is_written_once_and_the_cost_is_fresh(small, tx):
    main, _ = tx
    spend(main, 100_000)
    assert costs(small) == []
    for _ in range(2):
        pre_tx(main, "Bash", {"command": "uv run strateg budget set --usd 100"})
    assert len(costs(small)) == 1  # расход дочитан этим же вызовом: без него исчерпания не видно
    events = [e for e in journal_of(small) if e["kind"] == "бюджет" and e["actor"] == "хук"]
    assert len(events) == 1 and "хук спросил человека" in events[0]["message"]
    assert_clean(small)


@pytest.mark.parametrize(
    "command",
    [
        "uv run strateg budget set --usd 1",
        "uv run strateg budget set --usd 0.5",
        "uv run strateg budget set --us 0.5",
        "uv run strateg budget set --minutes 30 --usd 1",
        "uv run strateg budget check",
        "uv run strateg budget check --action выпуск",
    ],
)
def test_not_raising_the_limit_when_exhausted_is_not_asked(small, tx, command):
    main, _ = tx
    spend(main, 100_000)
    assert pre_tx(main, "Bash", {"command": command}) is None, command


def test_raising_the_limit_before_the_budget_runs_out_is_not_asked(small, tx):
    main, _ = tx
    spend(main, 50_000)
    assert pre_tx(main, "Bash", {"command": "uv run strateg budget set --usd 100"}) is None


def test_after_the_human_raised_the_limit_the_command_is_not_asked_again(cli, small, tx):
    main, _ = tx
    spend(main, 100_000)
    assert cli("budget", "set", "--usd", "5")[0] == 0  # человек вне хука
    assert pre_tx(main, "Bash", {"command": "uv run strateg budget set --usd 100"}) is None


def test_the_question_comes_in_the_hook_answer_for_powershell_too(small, tx):
    main, _ = tx
    spend(main, 100_000)
    raw = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "transcript_path": str(main),
            "tool_name": "PowerShell",
            "tool_input": {"command": "uv run strateg budget set --usd 100"},
        }
    ).encode("utf-8")
    out = json.loads(runner.run_pre_tool_use(raw, env()).stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and out["hookEventName"] == "PreToolUse"


@pytest.mark.parametrize(
    ("command", "step", "sets"),
    [
        ("uv run strateg step start 2а", True, ()),
        ("uv run strateg status && uv run strateg step done 1", True, ()),
        ("uv run strateg status", False, ()),
        ("uv run strateg budget set --usd 5 --minutes 9", False, (("--usd", "5", "--minutes", "9"),)),
        ("uv run strateg budget check", False, ()),
        ("python -m strateg.cli budget set --usd=5", False, (("--usd=5",),)),
        ("ls", False, ()),
        ("echo uv run strateg step done 1", False, ()),
    ],
)
def test_strateg_calls_find_step_and_budget_set(command, step, sets):
    from strateg.hooks import shell

    got = shell.strateg_calls(command)
    assert (got.step, got.budget_sets) == (step, sets), command


def test_non_paid_calls_are_recounted_not_more_often_than_every_five_seconds(small, tx, wall):
    main, _ = tx
    spend(main, 1000)
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert [r["message_id"] for r in costs(small)] == ["m1"]
    append(main, assistant("m2", SONNET, out=1000))
    wall[0] += 4.9
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert [r["message_id"] for r in costs(small)] == ["m1"]  # меньше 5 с: транскрипт не читали
    wall[0] += 0.2
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert [r["message_id"] for r in costs(small)] == ["m1", "m2"]


def test_paid_call_is_always_recounted(small, tx, wall):
    main, _ = tx
    spend(main, 50_000)
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    append(main, assistant("m2", SONNET, out=50_000))
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecision"] == "deny"
    assert [r["message_id"] for r in costs(small)] == ["m1", "m2"]


def test_exhaustion_recorded_before_is_still_refused_inside_the_five_seconds(small, tx, wall):
    main, _ = tx
    spend(main, 100_000)
    assert pre_tx(main, "WebSearch", {"query": "x"}, "researcher")["permissionDecision"] == "deny"
    wall[0] += 1
    assert pre_tx(main, "Bash", {"command": "ls"})["permissionDecision"] == "deny"


def test_the_time_of_the_last_recount_and_the_budget_state_are_kept_in_the_offsets_file(small, tx, wall):
    from strateg import accounting, offsets

    main, _ = tx
    assert offsets.recount_state(small) == (None, None)
    run_stop(main)
    wall[0] += 3
    assert offsets.recount_state(small) == (pytest.approx(3), False)
    wall[0] -= 10  # часы ушли назад: пересчёта «не было»
    assert offsets.recount_state(small) == (None, None)
    data = json.loads(accounting.offsets_path(small).read_text(encoding="utf-8"))
    assert data["recounted_at"] == 1_000_000.0 and data["exhausted"] is False and "files" in data
    wall[0] += 10
    spend(main, 100_000)
    run_stop(main)
    assert offsets.recount_state(small)[1] is True


def test_inside_the_window_a_budget_that_was_enough_is_not_read_again(small, tx, wall, monkeypatch):
    """Недавний пересчёт и «бюджета хватало»: проект заново не читается (задержка вызова)."""
    main, _ = tx
    spend(main, 1000)
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    loads = []
    real = proj.Project.load
    monkeypatch.setattr(
        proj.Project, "load", staticmethod(lambda root, timeout=None: loads.append(1) or real(root, timeout))
    )
    wall[0] += 1
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert loads == []
    wall[0] += 5
    assert pre_tx(main, "Bash", {"command": "ls"}) is None
    assert loads  # окно вышло: пересчёт и чтение проекта


@pytest.mark.parametrize(
    "tool", ["mcp__terminal__run_in_terminal", "Skill", "SendMessage", "ReadMcpResourceTool"]
)
def test_other_tools_of_the_main_agent_pass_as_without_the_hook_even_when_exhausted(small, tx, tool):
    """Вторая запись `PreToolUse`: основному агенту ни отказа, ни бюджета, ни записи; респонденту отказ."""
    main, _ = tx
    spend(main, 100_000)
    before = kinds(small)
    assert pre_tx(main, tool, {"command": "dir"}) is None
    assert kinds(small) == before and costs(small) == []  # расход не дочитывался: хук ничего не делал
    body = pre_tx(main, tool, {"command": "dir"}, "respondent")
    assert body["permissionDecision"] == "deny" and "Респонденту" in body["permissionDecisionReason"]


def test_a_refusal_by_budget_is_written_again_in_a_copy(small, tx, cli, home, tmp_path, clock):
    """Копия считает отказы от своего начала: отказ источника с тем же началом текста её не гасит."""
    main, _ = tx
    spend(main, 100_000)
    pre_tx(main, "WebSearch", {"query": "x"}, "researcher")
    assert len([e for e in journal_of(small) if e["kind"] == "бюджет" and e["actor"] == "хук"]) == 1
    clock.advance(60)
    assert cli("new", "cp", "--from", "demo")[0] == 0
    clock.advance(60)
    later = tmp_path / "claude2" / "later.jsonl"
    write(later, assistant("n1", SONNET, out=1_000_000))  # больше лимита копии ($8): расход копии
    pre_tx(later, "WebSearch", {"query": "x"}, "researcher", session_id="sess-2")
    events = [e for e in journal_of(home / "cp") if e["kind"] == "бюджет" and e["actor"] == "хук"]
    assert len(events) == 2  # отказ источника и отказ копии
    assert_clean(home / "cp")
