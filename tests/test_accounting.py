"""Запись стоимости по транскриптам в `cost.jsonl` (ADR-0010): без удвоения, с места, под блокировкой."""

from __future__ import annotations

import json

import pytest

from helpers import load_yaml
from strateg import accounting, storage
from strateg import project as proj
from strateg.project_check import check_project
from transcripts import OPUS, SONNET, agent_file, append, assistant, session_files, write


@pytest.fixture
def demo(cli, home):
    assert cli("new", "demo")[0] == 0
    return home / "demo"


@pytest.fixture
def files(tmp_path):
    return session_files(tmp_path / "claude")


def lines_of(root) -> list[dict]:
    text = (root / "cost.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main_source(path) -> list[accounting.Source]:
    return [accounting.Source("main", path, accounting.MAIN_ROLE)]


def assert_clean(root):
    """Запись хука не ломает проект: проверка чистая, хеши файлов в `state.yaml` верны."""
    assert check_project(root) == []
    assert proj.Project.load(root).outside == []


def test_new_messages_become_records_with_session_role_step_and_estimate(demo, files):
    main, _ = files
    write(
        main,
        assistant("m1", SONNET, inp=10, out=100, cache_read=1000, write_5m=200),
        assistant("m2", OPUS, out=50),
    )
    result = accounting.account(demo, "sess-1", main_source(main))
    assert (result.added, result.duplicates, result.skipped) == (2, 0, 0)
    first, second = lines_of(demo)
    assert first["message_id"] == "m1" and first["session_id"] == "sess-1" and first["role"] == "основной"
    assert first["step"] == "1" and first["entry"] == "по сообщению" and first["environment"] == "claude-code"
    assert first["model"] == SONNET and first["cost_source"] == "оценка"
    assert (first["input_tokens"], first["output_tokens"]) == (10, 100)
    assert (first["cache_read_tokens"], first["cache_write_5m_tokens"]) == (1000, 200)
    assert first["cost_usd"] == pytest.approx((10 * 2 + 100 * 10 + 1000 * 0.1 + 200 * 2.5) / 1e6)
    assert second["model"] == OPUS and second["cost_usd"] == pytest.approx(50 * 20 / 1e6)
    assert_clean(demo)


def test_record_has_numbers_ids_and_model_only(demo, files):
    """Персональные данные: в `cost.jsonl` нет текста разговора, даже если он есть в транскрипте."""
    main, _ = files
    record = {
        "type": "assistant",
        "cwd": "C:\\Users\\Ivan\\проект",
        "message": {
            "id": "m1",
            "model": SONNET,
            "content": [{"type": "text", "text": "ЛИЧНЫЕ ДАННЫЕ КЛИЕНТА"}],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    }
    write(main, json.dumps(record, ensure_ascii=False))
    accounting.account(demo, "sess-1", main_source(main))
    text = (demo / "cost.jsonl").read_text(encoding="utf-8") + (demo / "journal.jsonl").read_text(
        encoding="utf-8"
    )
    assert "ЛИЧНЫЕ" not in text and "Ivan" not in text
    offsets = accounting.offsets_path(demo).read_text(encoding="utf-8")
    assert "ЛИЧНЫЕ" not in offsets and "Ivan" not in offsets


def test_repeated_parse_does_not_double_the_cost(demo, files):
    main, _ = files
    write(main, assistant("m1", out=100), assistant("m1", out=100), assistant("m2", out=10))
    accounting.account(demo, "sess-1", main_source(main))
    total_once = proj.cost_total(proj.Project.load(demo).costs)[0]
    again = accounting.account(demo, "sess-1", main_source(main))
    assert again.added == 0
    assert len(lines_of(demo)) == 2
    assert proj.cost_total(proj.Project.load(demo).costs)[0] == total_once
    assert_clean(demo)


def test_lost_offsets_do_not_double_the_cost(demo, files):
    """Файл смещений удалён: разбор идёт с начала, но id уже в `cost.jsonl` — расход прежний."""
    main, _ = files
    write(main, assistant("m1", out=100), assistant("m2", out=10))
    accounting.account(demo, "sess-1", main_source(main))
    accounting.offsets_path(demo).unlink()
    again = accounting.account(demo, "sess-1", main_source(main))
    assert (again.added, again.duplicates) == (0, 2) and len(lines_of(demo)) == 2


def test_parse_continues_from_the_previous_position(demo, files):
    main, _ = files
    write(main, assistant("m1", out=1))
    accounting.account(demo, "sess-1", main_source(main))
    append(main, assistant("m2", out=2))
    result = accounting.account(demo, "sess-1", main_source(main))
    assert (result.added, result.duplicates) == (1, 0)  # старая часть файла не читалась
    assert [r["message_id"] for r in lines_of(demo)] == ["m1", "m2"]


def test_truncated_file_is_parsed_again_without_doubling(demo, files):
    main, _ = files
    write(main, *[assistant(f"m{i}", out=i) for i in range(1, 5)])
    accounting.account(demo, "sess-1", main_source(main))
    write(main, assistant("m1", out=1), assistant("m5", out=5))  # переписан и стал короче
    result = accounting.account(demo, "sess-1", main_source(main))
    assert (result.added, result.duplicates) == (1, 1)
    assert [r["message_id"] for r in lines_of(demo)] == ["m1", "m2", "m3", "m4", "m5"]
    assert_clean(demo)


def test_same_message_in_another_session_is_not_counted_twice(demo, files):
    """Ответвление и продолжение сессии копируют историю с теми же id сообщений."""
    main, _ = files
    write(main, assistant("m1", out=100))
    accounting.account(demo, "sess-1", main_source(main))
    copy = write(main.with_name("sess-2.jsonl"), assistant("m1", out=100), assistant("m2", out=5))
    accounting.account(demo, "sess-2", [accounting.Source("main2", copy, accounting.MAIN_ROLE)])
    assert [(r["session_id"], r["message_id"]) for r in lines_of(demo)] == [
        ("sess-1", "m1"),
        ("sess-2", "m2"),
    ]
    assert_clean(demo)


def test_broken_lines_are_counted_in_the_service_file_not_in_the_cost(demo, files):
    main, _ = files
    write(main, "не json", assistant("m1", out=1), '{"type": "assistant", "message": {"id": "x"}}')
    result = accounting.account(demo, "sess-1", main_source(main))
    assert (result.added, result.skipped) == (1, 2)
    assert accounting.skipped_total(demo) == 2
    append(main, "ещё одна битая строка")
    accounting.account(demo, "sess-1", main_source(main))
    assert accounting.skipped_total(demo) == 3
    assert_clean(demo)


def test_skipped_count_does_not_grow_when_the_file_is_read_again_from_the_start(demo, files):
    main, _ = files
    write(main, "битая", assistant("m1", out=1))
    accounting.account(demo, "sess-1", main_source(main))
    write(main, "другая битая строка", assistant("m1", out=1))  # файл переписан: другое начало
    accounting.account(demo, "sess-1", main_source(main))
    assert accounting.skipped_total(demo) == 1


def test_model_without_a_price_keeps_tokens_and_marks_the_sum_as_missing(demo, files):
    main, _ = files
    write(main, assistant("m1", "claude-future-9", inp=7, out=9), assistant("m2", SONNET, out=1))
    result = accounting.account(demo, "sess-1", main_source(main))
    assert result.unpriced == ["claude-future-9"]
    unpriced, priced = lines_of(demo)
    assert unpriced["cost_source"] == "нет данных" and "cost_usd" not in unpriced
    assert (unpriced["input_tokens"], unpriced["output_tokens"]) == (7, 9)
    total, precision, complete = proj.cost_total(proj.Project.load(demo).costs)
    assert total == pytest.approx(priced["cost_usd"]) and precision == "оценка" and complete is False
    assert_clean(demo)


def test_web_search_goes_into_the_record(demo, files):
    main, _ = files
    write(main, assistant("m1", SONNET, out=0, web=3))
    accounting.account(demo, "sess-1", main_source(main))
    (rec,) = lines_of(demo)
    assert rec["web_searches"] == 3 and rec["cost_usd"] == pytest.approx(0.03)


def test_step_is_taken_from_state_at_the_time_of_accounting(cli, demo, files):
    main, _ = files
    write(main, assistant("m1", out=1))
    accounting.account(demo, "sess-1", main_source(main))
    assert cli("step", "start", "2а")[0] == 0
    append(main, assistant("m2", out=1))
    accounting.account(demo, "sess-1", main_source(main))
    assert [r["step"] for r in lines_of(demo)] == ["1", "2а"]


def test_subagent_records_carry_the_role_and_the_main_transcript_is_not_read_twice(demo, files):
    main, folder = files
    write(main, assistant("m1", out=1))
    sub = agent_file(folder, "a1", assistant("s1", out=2), assistant("s2", out=3))
    sources = [
        accounting.Source("main", main, accounting.MAIN_ROLE),
        accounting.Source("agent:a1", sub, "researcher"),
    ]
    accounting.account(demo, "sess-1", sources)
    accounting.account(demo, "sess-1", sources)
    got = [(r["role"], r["message_id"]) for r in lines_of(demo)]
    assert got == [("основной", "m1"), ("researcher", "s1"), ("researcher", "s2")]


def test_missing_required_transcript_is_a_failure_and_writes_nothing(demo, files):
    main, _ = files
    with pytest.raises(FileNotFoundError):
        accounting.account(demo, "sess-1", main_source(main))
    assert lines_of(demo) == []


def test_missing_optional_transcript_is_skipped(demo, files):
    main, _ = files
    write(main, assistant("m1", out=1))
    sources = [
        accounting.Source("main", main, accounting.MAIN_ROLE),
        accounting.Source("agent:gone", main.with_name("none.jsonl"), "critic", required=False),
    ]
    assert accounting.account(demo, "sess-1", sources).added == 1


def test_cost_hash_in_state_follows_the_append(demo, files):
    """Запись хука не даёт ложного «изменено вне агента»: хеш `cost.jsonl` в `state.yaml` обновлён."""
    main, _ = files
    write(main, assistant("m1", out=1))
    accounting.account(demo, "sess-1", main_source(main))
    state = load_yaml(demo / "state.yaml")
    assert state["files"]["cost.jsonl"] == storage.sha256((demo / "cost.jsonl").read_bytes())
    assert state["files"]["journal.jsonl"] == storage.sha256((demo / "journal.jsonl").read_bytes())


def test_status_line_sees_fresh_records(cli, demo, files):
    """Строка статуса берёт расход из `cost.jsonl`: хуки досчитали — строка видит сразу."""
    main, _ = files
    assert cli("budget", "set", "--usd", "1")[0] == 0
    write(main, assistant("m1", out=85_000))  # $0,85 из $1
    accounting.account(demo, "sess-1", main_source(main))
    code, out, _ = cli("status", "--line")
    assert code == 0 and "$0,85 из $1" in out and "85 % бюджета" in out
    append(main, assistant("m2", out=15_000))
    accounting.account(demo, "sess-1", main_source(main))
    code, out, _ = cli("status", "--line")
    assert "$1,00 из $1" in out and "бюджет исчерпан: деньги" in out


def test_status_line_is_quiet_below_the_warning_share(cli, demo, files):
    main, _ = files
    assert cli("budget", "set", "--usd", "1")[0] == 0
    write(main, assistant("m1", out=79_000))
    accounting.account(demo, "sess-1", main_source(main))
    code, out, _ = cli("status", "--line")
    assert "$0,79 из $1" in out and "бюджета" not in out and "исчерпан" not in out


def test_sources_for_event_find_main_own_and_known_swept_agents(tmp_path):
    main, folder = session_files(tmp_path)
    write(main, assistant("m1"))
    agent_file(folder, "known", assistant("a"), agent_type="researcher")
    agent_file(folder, "unknown", assistant("b"))
    agent_file(folder, "me", assistant("c"))
    event = {"transcript_path": str(main), "session_id": "s7", "agent_id": "me", "agent_type": "critic"}
    got = {s.key: s.role for s in accounting.sources_for(event, main=True, sweep=True)}
    assert got == {"main:s7": "основной", "agent:me": "critic", "agent:known": "researcher"}
    only_own = {s.key: s.role for s in accounting.sources_for(event, main=False, sweep=False)}
    assert only_own == {"agent:me": "critic"}


def test_subagent_stop_event_names_the_transcript_path(tmp_path):
    main, folder = session_files(tmp_path)
    path = agent_file(folder, "x1", assistant("a"))
    event = {
        "transcript_path": str(main),
        "agent_id": "x1",
        "agent_type": "",
        "agent_transcript_path": str(path),
    }
    (src,) = accounting.sources_for(event, main=False, sweep=False)
    assert src.path == path and src.role == accounting.INTERNAL_ROLE  # пустой agent_type — служебный агент
