"""Хуки на настоящем проекте: журнал, модель роли, SessionStart, PostToolUseFailure, сбои, переменные."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers import FIXTURE, journal_of
from strateg import project as proj
from strateg.hooks import effects, rules, runner
from strateg.project_check import check_project

SESSION = "sess-1"
EMPTY_TRANSCRIPT = str(Path(__file__).parent / "fixtures" / "transcripts" / "empty.jsonl")


def env(**extra) -> dict[str, str]:
    base = {
        k: v for k, v in os.environ.items() if not k.startswith("STRATEG_") or k == "STRATEG_PROJECTS_DIR"
    }
    base.update(extra)
    return base


def event(tool, tin=None, agent=None, name="PreToolUse", session=SESSION, **extra):
    data = {
        "hook_event_name": name,
        "session_id": session,
        "transcript_path": EMPTY_TRANSCRIPT,  # платный вызов дочитывает транскрипт: он должен быть
        "tool_name": tool,
        "tool_input": tin or {},
    }
    if agent:
        data["agent_type"] = agent
        data["agent_id"] = "a-1"
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def pre(tool, tin=None, agent=None, e=None, **extra):
    out = runner.run_pre_tool_use(event(tool, tin, agent, **extra), e or env()).stdout
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    """Хук пишет время теми же часами, что и CLI: иначе события шли бы не по порядку."""
    monkeypatch.setattr(effects, "now", clock)


@pytest.fixture
def demo(cli, home):
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def kinds(root):
    return [e["kind"] for e in journal_of(root)]


def assert_clean(root):
    """Хук не ломает проект: проверка чистая и хеш журнала в `state.yaml` совпадает."""
    assert check_project(root) == []
    p = proj.Project.load(root)
    assert p.outside == [], p.outside


# --- отказ пишется в журнал ---


def test_refusal_is_written_to_the_journal_by_hook(demo):
    body = pre("WebFetch", {"url": "https://example.com", "prompt": "x"})
    assert body["permissionDecision"] == "deny" and "WebFetch" in body["permissionDecisionReason"]
    last = journal_of(demo)[-1]
    assert last["kind"] == "отказ хука" and last["actor"] == "хук" and last["name"] == "WebFetch"
    assert last["message"] == body["permissionDecisionReason"] and last["step"] == "1"
    assert_clean(demo)


def test_refusal_names_the_role_in_the_journal(demo):
    pre("Bash", {"command": "curl https://example.com"}, "researcher")
    last = journal_of(demo)[-1]
    assert last["kind"] == "отказ хука" and last["role"] == "researcher" and last["name"] == "Bash"
    assert_clean(demo)


def test_hook_events_do_not_look_like_outside_changes(demo, cli):
    """Следующая команда CLI не пишет «изменено вне агента» после записи хука."""
    pre("WebFetch", {"url": "https://example.com"})
    pre("WebFetch", {"url": "https://example.com"})
    assert cli("status")[0] == 0
    assert "изменено вне агента" not in kinds(demo)
    assert cli("step", "start", "1")[0] == 0
    assert "изменено вне агента" not in kinds(demo)
    assert_clean(demo)


def test_hook_does_not_accept_foreign_changes(demo, cli):
    """Хеш журнала хук обновляет сам, остальных файлов не трогает: правку человека увидит `status`."""
    (demo / "sources.yaml").write_bytes((demo / "sources.yaml").read_bytes() + b"\n")
    pre("WebFetch", {"url": "https://example.com"})
    p = proj.Project.load(demo)
    assert [rel for rel, _ in p.outside] == ["sources.yaml"]


def test_hook_does_not_hide_hand_edited_journal(demo, cli):
    journal = demo / "journal.jsonl"
    journal.write_bytes(journal.read_bytes() + b"\n")  # журнал правили руками: хеш не принимаем
    pre("WebFetch", {"url": "https://example.com"})
    assert [rel for rel, _ in proj.Project.load(demo).outside] == ["journal.jsonl"]


def test_no_project_means_no_journal_and_no_crash(home):
    body = pre("WebFetch", {"url": "https://example.com"})
    assert body["permissionDecision"] == "deny"
    assert not home.exists() or not any(home.iterdir())


def test_broken_project_does_not_turn_refusal_into_crash(demo):
    (demo / "state.yaml").write_text("не yaml: [", encoding="utf-8")
    body = pre("WebFetch", {"url": "https://example.com"})
    assert body["permissionDecision"] == "deny" and "WebFetch" in body["permissionDecisionReason"]


# --- модель роли: событие route есть, нет, повышение ---


def choose_events(root):
    return [e for e in journal_of(root) if e["kind"] == "выбор модели"]


def test_launch_without_route_event_hook_chooses_and_writes_one_event(demo):
    body = pre("Agent", {"subagent_type": "critic", "prompt": "режим: полнота; папка projects/demo"})
    assert body["permissionDecision"] == "allow" and body["updatedInput"]["model"] == "sonnet"
    events = choose_events(demo)
    assert len(events) == 1
    e = events[0]
    assert (
        e["actor"] == "хук" and e["role"] == "critic" and e["model"] == "sonnet" and e["level"] == "средняя"
    )
    assert e["effort"] == "high" and "route не вызван" in e["message"] and e["name"] == "выбор"
    assert_clean(demo)


def test_second_launch_on_same_step_does_not_duplicate_the_event(demo):
    for _ in range(3):
        pre("Agent", {"subagent_type": "critic", "prompt": "режим: полнота"})
    assert len(choose_events(demo)) == 1
    assert_clean(demo)


def test_route_event_from_skill_is_used_and_not_duplicated(demo, cli):
    assert cli("route", "researcher", "--reason", "вопрос ВП-1")[0] == 0
    before = len(choose_events(demo))
    body = pre("Agent", {"subagent_type": "researcher", "prompt": "Вопрос: цена; предмет: тариф"})
    assert body["updatedInput"]["model"] == "sonnet"
    assert len(choose_events(demo)) == before
    assert_clean(demo)


def test_upgrade_event_wins_over_the_table(demo, cli):
    """После `route critic --upgrade` хук берёт повышенную модель из журнала, а не уровень по таблице."""
    from helpers import edit_journal

    assert cli("route", "critic")[0] == 0

    def make_upgrade(events):
        last = events[-1]
        events.append({**last, "name": "повышение", "model": "opus", "level": "сильная"})

    edit_journal(demo, make_upgrade)
    from helpers import rehash

    rehash(demo)
    body = pre("Agent", {"subagent_type": "critic", "prompt": "режим: полнота"})
    assert body["updatedInput"]["model"] == "opus"
    assert len(choose_events(demo)) == 2
    assert_clean(demo)


def test_profile_changes_the_model_chosen_by_hook(demo, cli):
    assert cli("route", "--profile", "глубже")[0] == 0
    body = pre("Agent", {"subagent_type": "critic", "prompt": "режим: полнота"})
    assert body["updatedInput"]["model"] == "opus"
    e = choose_events(demo)[-1]
    assert e["level"] == "сильная" and "профиль глубже" in e["message"]
    assert_clean(demo)


def test_route_called_after_hook_is_still_fine(demo, cli):
    pre("Agent", {"subagent_type": "critic", "prompt": "режим: полнота"})
    assert cli("route", "critic", "--reason", "после хука")[0] == 0
    assert_clean(demo)


def test_builtin_subagent_refused_with_journal_event_and_role_list(demo):
    body = pre("Agent", {"subagent_type": "general-purpose", "prompt": "найди"})
    assert body["permissionDecision"] == "deny" and "researcher" in body["permissionDecisionReason"]
    assert kinds(demo)[-1] == "отказ хука"
    assert not choose_events(demo)


def test_real_roles_have_wrapper_and_routing_key():
    live = effects.LiveServices(rules.Ctx(penv=effects.make_ctx({}, env()).penv, env=env()))
    wrappers = live.agents()
    assert {"researcher", "critic", "classifier", "respondent"} <= wrappers
    routing = live.routing()
    assert {"researcher", "critic", "classifier", "respondent"} <= set(routing.roles)


def test_role_without_wrapper_is_refused_not_crash(demo):
    body = pre("Agent", {"subagent_type": "interviewer", "prompt": "побеседуй"})
    assert body["permissionDecision"] == "deny"


def test_respondent_launch_gets_the_routing_model_and_its_tools_are_closed(demo):
    body = pre("Agent", {"subagent_type": "respondent", "prompt": "## Персона"})
    assert body["permissionDecision"] == "allow" and body["updatedInput"]["model"] == "sonnet"
    for tool, tin in (("Read", {"file_path": "README.md"}), ("Bash", {"command": "ls"}), ("Glob", {})):
        body = pre(tool, tin, agent="respondent")
        assert body["permissionDecision"] == "deny" and "Респонденту" in body["permissionDecisionReason"]
    assert kinds(demo)[-1] == "отказ хука"


def test_classifier_launch_gets_the_cheap_model_and_its_tools_are_closed(demo):
    body = pre("Agent", {"subagent_type": "classifier", "prompt": "## Пары"})
    assert body["permissionDecision"] == "allow" and body["updatedInput"]["model"] == "haiku"
    for tool, tin in (("Read", {"file_path": "README.md"}), ("Bash", {"command": "ls"}), ("Glob", {})):
        body = pre(tool, tin, agent="classifier")
        assert body["permissionDecision"] == "deny" and "Классификатору" in body["permissionDecisionReason"]
    assert kinds(demo)[-1] == "отказ хука"


def test_routing_table_missing_is_refusal_with_reason(demo, tmp_path, monkeypatch):
    empty = tmp_path / "no-config"
    empty.mkdir()
    monkeypatch.setenv("STRATEG_CONFIG_DIR", str(empty))
    body = pre("Agent", {"subagent_type": "critic", "prompt": "x"})
    assert body["permissionDecision"] == "deny" and "routing.yaml" in body["permissionDecisionReason"]


# --- задание исследователю ---


@pytest.fixture
def example(home, clock):
    shutil.copytree(FIXTURE, home / "example")
    proj.set_active("example")
    return home / "example"


def test_task_with_plan_question_is_logged(example):
    body = pre(
        "Agent", {"subagent_type": "researcher", "prompt": "Вопрос ВП-2: цена; предмет: тариф; период 2026"}
    )
    assert body["permissionDecision"] == "allow", body
    tasks = [e for e in journal_of(example) if e["kind"] == "задание исследователю"]
    assert len(tasks) == 1 and "ВП-2" in tasks[0]["message"] and tasks[0]["actor"] == "хук"
    assert tasks[0]["role"] == "researcher"
    assert proj.Project.load(example).journal[-1].kind == "задание исследователю"


def test_task_with_unknown_plan_question_refused_and_not_logged(example):
    body = pre("Agent", {"subagent_type": "researcher", "prompt": "Вопрос ВП-99: цена"})
    assert body["permissionDecision"] == "deny" and "ВП-99" in body["permissionDecisionReason"]
    assert not [e for e in journal_of(example) if e["kind"] == "задание исследователю"]
    assert journal_of(example)[-1]["kind"] == "отказ хука"


def test_task_with_inputs_path_refused(example):
    body = pre(
        "Agent", {"subagent_type": "researcher", "prompt": "Посмотри projects/example/inputs/brief.txt"}
    )
    assert body["permissionDecision"] == "deny" and "inputs/" in body["permissionDecisionReason"]


def test_task_text_is_data_secret_lines_are_cut(example):
    prompt = "Вопрос ВП-1: цена\nключ sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\nпериод 2026"
    body = pre("Agent", {"subagent_type": "researcher", "prompt": prompt})
    assert body["permissionDecision"] == "allow"
    text = (example / "journal.jsonl").read_text(encoding="utf-8")
    assert "sk-ant-api03" not in text and effects.HIDDEN_LINE in text


def test_task_with_shell_text_is_just_text(example, tmp_path):
    """Задание не попадает в shell: подстановки остаются обычным текстом журнала."""
    marker = tmp_path / "pwned"
    prompt = f"Вопрос ВП-1: $(touch {marker}) `touch {marker}`; цена"
    body = pre("Agent", {"subagent_type": "researcher", "prompt": prompt})
    assert body["permissionDecision"] == "allow"
    assert not marker.exists()
    assert "$(touch" in journal_of(example)[-1]["message"]


# --- вход не Claude Code ---


def foreign_bytes(session=None):
    data = {
        "hook_event_name": "preToolUse",
        "cursor_version": "3.0",
        "conversation_id": "c",
        "tool_name": "WebFetch",
    }
    data["tool_input"] = {"url": "https://example.com"}
    if session:
        data["session_id"] = session
    return json.dumps(data).encode("utf-8")


def test_not_claude_code_passes_and_writes_event_once_per_session(demo):
    for _ in range(3):
        assert runner.run_pre_tool_use(foreign_bytes("s-1"), env()).stdout == ""
    notes = [e for e in journal_of(demo) if e["kind"] == "граница прав не активна"]
    assert len(notes) == 1 and notes[0]["name"] == "s-1" and notes[0]["actor"] == "хук"
    runner.run_pre_tool_use(foreign_bytes("s-2"), env())
    assert len([e for e in journal_of(demo) if e["kind"] == "граница прав не активна"]) == 2
    assert_clean(demo)


def test_guard_still_works_outside_claude_code(demo):
    data = {"hook_event_name": "preToolUse", "conversation_id": "c", "tool_name": "Read"}
    data["tool_input"] = {"file_path": ".env"}
    out = runner.run_pre_tool_use(json.dumps(data).encode(), env()).stdout
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- STRATEG_HOOKS=0 и сбой хука прав ---


def test_hooks_off_variable_disables_everything(demo):
    off = env(STRATEG_HOOKS="0")
    assert runner.run_pre_tool_use(event("WebFetch", {"url": "https://x.com"}), off).stdout == ""
    assert runner.run_session_start(event("x", name="SessionStart", source="startup"), off).stdout == ""
    runner.run_post_tool_use_failure(event("Bash", name="PostToolUseFailure", error="сбой"), off)
    assert kinds(demo) == ["действие"]  # только «проект создан»


def test_hooks_off_value_must_be_exactly_zero(demo):
    for value in ("1", "", "false"):
        out = runner.run_pre_tool_use(
            event("WebFetch", {"url": "https://x.com"}), env(STRATEG_HOOKS=value)
        ).stdout
        assert "deny" in out


def test_hook_crash_blocks_the_call(monkeypatch, demo):
    def boom(*a, **k):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(rules, "decide_pre_tool_use", boom)
    body = pre("Read", {"file_path": "README.md"})
    assert body["permissionDecision"] == "deny"
    assert body["permissionDecisionReason"].startswith("хук прав не сработал: RuntimeError: что-то сломалось")


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
def test_bad_input_blocks_the_call(raw):
    out = runner.run_pre_tool_use(raw, env()).stdout
    body = json.loads(out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny" and "хук прав не сработал" in body["permissionDecisionReason"]


def test_services_failure_blocks_launch(monkeypatch, demo):
    monkeypatch.setattr(effects.LiveServices, "agents", lambda self: 1 / 0)
    body = pre("Agent", {"subagent_type": "critic", "prompt": "x"})
    assert body["permissionDecision"] == "deny" and "хук прав не сработал" in body["permissionDecisionReason"]


def test_journal_write_failure_blocks_role_launch_but_not_refusal(monkeypatch, demo):
    def boom(*a, **k):
        raise OSError("диск")

    monkeypatch.setattr(effects, "append_event", boom)
    body = pre("Agent", {"subagent_type": "critic", "prompt": "x"})
    assert body["permissionDecision"] == "deny" and "хук прав не сработал" in body["permissionDecisionReason"]
    body = pre("WebFetch", {"url": "https://x.com"})
    assert body["permissionDecision"] == "deny" and "WebFetch" in body["permissionDecisionReason"]


# --- SessionStart ---


def session(source, e=None):
    out = runner.run_session_start(event("x", name="SessionStart", source=source), e or env()).stdout
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    return body["additionalContext"]


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_session_start_gives_brief(demo, source):
    text = session(source)
    assert "Проект: demo" in text and "Шаг: 1" in text and "status --brief" in text
    assert "--peek" not in text


def test_session_start_compact_is_peek_and_writes_nothing(demo, cli):
    assert cli("step", "start", "1")[0] == 0
    before = journal_of(demo)
    text = session("compact")
    assert "--peek" in text and "Проект: demo" in text
    assert journal_of(demo) == before  # идущий шаг прерванным не становится (замечание о PreCompact)
    assert not any(e.get("status") == "прервано" for e in journal_of(demo))


def test_session_start_fork_is_peek_and_writes_nothing(demo, cli):
    assert cli("step", "start", "1")[0] == 0
    before = journal_of(demo)
    text = session("fork")
    assert "--peek" in text and "Проект: demo" in text
    assert journal_of(demo) == before
    assert not any(e.get("status") == "прервано" for e in journal_of(demo))


def test_session_start_startup_records_interrupted_step(demo, cli):
    assert cli("step", "start", "1")[0] == 0
    session("startup")
    assert any(e.get("status") == "прервано" for e in journal_of(demo))
    assert_clean(demo)


def test_session_start_without_project_is_one_line(home):
    text = session("startup")
    assert text == "Проекта нет, начни с `strateg new`."
    assert session("compact") == text
    assert session("fork") == text


def test_session_start_failure_does_not_block(demo, monkeypatch):
    monkeypatch.setattr(runner, "session_summary", lambda source: 1 / 0)
    result = runner.run_session_start(event("x", name="SessionStart", source="startup"), env())
    assert result.stdout == "" and result.exit_code == 0
    assert runner.run_session_start(b"garbage", env()).exit_code == 0


def test_session_start_output_is_only_json(demo, capsys):
    runner.run_session_start(event("x", name="SessionStart", source="startup"), env())
    assert capsys.readouterr().out == ""  # всё, что печатает CLI, перехвачено


# --- PostToolUseFailure ---


def failure(error, tool="Bash", e=None, **extra):
    raw = event(tool, {"command": "x"}, name="PostToolUseFailure", error=error, **extra)
    return runner.run_post_tool_use_failure(raw, e or env())


def test_failure_is_written_to_the_journal(demo):
    assert failure("Exit code 1\nCannot find module 'express'").exit_code == 0
    last = journal_of(demo)[-1]
    assert last["kind"] == "ошибка" and last["actor"] == "хук" and last["name"] == "Bash"
    assert "Cannot find module" in last["message"]
    assert_clean(demo)


def test_failure_text_is_clipped(demo):
    failure("x" * 5000)
    message = journal_of(demo)[-1]["message"]
    assert len(message) <= effects.MAX_ERROR_CHARS and message.endswith("…")


def test_failure_key_like_lines_are_cut(demo):
    failure(
        "Ошибка запроса\nAuthorization: Bearer abcdefghij1234567890abcdefghij\nтретья строка\n"
        "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"
    )
    message = journal_of(demo)[-1]["message"]
    assert "Bearer" not in message and "sk-ant" not in message
    assert "Ошибка запроса" in message and "третья строка" in message and effects.HIDDEN_LINE in message


def test_failure_text_starting_with_dashes_is_data(demo):
    failure("--message --tool injected")
    last = journal_of(demo)[-1]
    assert (
        last["kind"] == "ошибка" and last["name"] == "Bash" and last["message"] == "--message --tool injected"
    )


def test_failure_shell_text_is_not_executed(demo, tmp_path):
    marker = tmp_path / "pwned"
    failure(f"$(touch {marker}); `touch {marker}`")
    assert not marker.exists()
    assert "$(touch" in journal_of(demo)[-1]["message"]


def test_failure_in_role_names_it(demo):
    failure("нет доступа", tool="Read", agent_type="researcher")
    assert journal_of(demo)[-1]["message"] == "[researcher] нет доступа"


def test_failure_without_text(demo):
    failure("")
    assert journal_of(demo)[-1]["message"] == "ошибка инструмента без текста"


def test_interrupt_is_not_a_failure(demo):
    failure("прервано", is_interrupt=True)
    assert kinds(demo) == ["действие"]


def test_failure_without_project_does_nothing(home):
    assert failure("сбой").exit_code == 0
    assert not home.exists() or not any(home.iterdir())


def test_failure_hook_never_blocks(demo, monkeypatch):
    monkeypatch.setattr(runner, "_run_cli", lambda argv: 1 / 0)
    assert failure("сбой").exit_code == 0
    assert runner.run_post_tool_use_failure(b"junk", env()).exit_code == 0
