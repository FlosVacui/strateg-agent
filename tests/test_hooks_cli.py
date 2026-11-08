"""CLI рядом с хуками: строка статуса без проекта, переменные в doctor и status --brief, новые события."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import journal_of
from strateg import models
from strateg.cli import main
from strateg.hooks import flags

# --- строка статуса (С-13) ---


def test_status_line_without_project_is_not_an_error(cli, home, capsys):
    code, out, err = cli("status", "--line")
    assert code == 0 and out.strip() == "Стратег: проекта нет" and err == ""
    code, out, _ = cli("status", "--line", "--json")
    assert code == 0 and json.loads(out) == {"ok": True, "line": "Стратег: проекта нет", "project": None}


def test_status_line_without_project_is_one_line_without_newlines(cli, home):
    assert cli("status", "--line")[1].count("\n") == 1


def test_status_line_with_project_is_unchanged(cli, home):
    cli("new", "demo")
    code, out, _ = cli("status", "--line")
    assert code == 0 and out.startswith("Шаг 1 из 8 · Постановка")


def test_other_status_forms_without_project_still_fail(cli, home):
    for argv in (("status",), ("status", "--brief")):
        code, _, err = cli(*argv)
        assert code == 1 and "активный проект не выбран" in err


def test_status_line_for_missing_explicit_project_is_still_an_error(cli, home):
    code, _, err = cli("status", "--line", "--project", "nope")
    assert code == 1 and "проект не найден" in err


# --- переменные: doctor и status --brief ---


def test_flag_notes():
    assert flags.notes({}) == []
    assert flags.notes({"STRATEG_HOOKS": "1", "STRATEG_DEV": "0"}) == []
    assert "STRATEG_HOOKS=0" in flags.notes({"STRATEG_HOOKS": "0"})[0]
    assert "STRATEG_DEV=1" in flags.notes({"STRATEG_DEV": "1"})[0]
    assert len(flags.notes({"STRATEG_HOOKS": "0", "STRATEG_DEV": "1"})) == 2


def test_doctor_is_silent_without_variables(capsys, monkeypatch):
    monkeypatch.delenv("STRATEG_HOOKS", raising=False)
    monkeypatch.delenv("STRATEG_DEV", raising=False)
    assert main(["doctor"]) == 0
    assert "Хуки" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "name,value,needle",
    [("STRATEG_HOOKS", "0", "все хуки выключены"), ("STRATEG_DEV", "1", "охрана правил")],
)
def test_doctor_shows_the_variables(capsys, monkeypatch, name, value, needle):
    monkeypatch.setenv(name, value)
    assert main(["doctor"]) == 0  # предупреждение код не меняет
    out = capsys.readouterr().out
    assert "Хуки" in out and name in out and needle in out and "ПРЕД" in out


def test_status_brief_shows_the_variables(cli, home, monkeypatch):
    cli("new", "demo")
    monkeypatch.delenv("STRATEG_HOOKS", raising=False)
    monkeypatch.delenv("STRATEG_DEV", raising=False)
    quiet = cli("status", "--brief")[1]
    assert "STRATEG_" not in quiet and "Хуки" not in quiet
    monkeypatch.setenv("STRATEG_HOOKS", "0")
    monkeypatch.setenv("STRATEG_DEV", "1")
    out = cli("status", "--brief")[1]
    assert "Внимание:" in out and "STRATEG_HOOKS=0" in out and "STRATEG_DEV=1" in out
    assert len(out.splitlines()) <= 10
    data = json.loads(cli("status", "--brief", "--peek", "--json")[1])
    assert len(data["env_notes"]) == 2


def test_brief_with_variables_stays_within_ten_lines_when_everything_happens(cli, home, monkeypatch, clock):
    """Прерванный шаг, правка вне агента и обе переменные: сводка всё равно не длиннее десяти строк."""
    cli("new", "demo")
    cli("step", "start", "1")
    (home / "demo" / "sources.yaml").write_bytes((home / "demo" / "sources.yaml").read_bytes() + b"\n")
    monkeypatch.setenv("STRATEG_HOOKS", "0")
    monkeypatch.setenv("STRATEG_DEV", "1")
    out = cli("status", "--brief")[1]
    assert len(out.splitlines()) <= 10


# --- новые события журнала ---


def event(kind, **kw):
    base = {
        "schema_version": 1,
        "time": "2026-11-06T10:00:00+03:00",
        "actor": "хук",
        "kind": kind,
        "step": "2б",
    }
    return models.JournalEvent.model_validate({**base, **kw})


@pytest.mark.parametrize("kind", ["отказ хука", "граница прав не активна", "задание исследователю"])
def test_new_kinds_need_a_message(kind):
    event(kind, message="текст")
    with pytest.raises(ValueError, match="нет пояснения"):
        event(kind)


def test_new_kinds_do_not_take_action_fields():
    with pytest.raises(ValueError):
        event("отказ хука", message="x", status="выполнено")
    with pytest.raises(ValueError):
        event("отказ хука", message="x", level="средняя")


def test_hook_choice_event_is_a_valid_model_choice():
    e = event(
        "выбор модели",
        name="выбор",
        role="critic",
        model="sonnet",
        level="средняя",
        effort="high",
        message="m",
    )
    assert e.actor == "хук" and e.role == "critic"


def test_journal_schema_lists_the_new_kinds():
    path = Path(__file__).resolve().parent.parent / "schemas" / "journal-event.schema.json"
    kinds = json.loads(path.read_text(encoding="utf-8"))["properties"]["kind"]["enum"]
    for kind in ("отказ хука", "граница прав не активна", "задание исследователю"):
        assert kind in kinds


def test_hook_events_pass_the_project_check(cli, home):
    """Журнал с событиями хука проходит `strateg check`."""
    from strateg.hooks import effects, rules

    cli("new", "demo")
    root = home / "demo"
    for spec in (
        rules.EventSpec("отказ хука", "WebFetch закрыт", name="WebFetch"),
        rules.EventSpec("граница прав не активна", "вход не Claude Code", name="s-1"),
        rules.EventSpec("задание исследователю", "Вопрос ВП-1", role="researcher"),
        rules.EventSpec("выбор модели", "route не вызван", name="выбор", role="critic", model="sonnet",
                        level="средняя", effort="high"),
    ):  # fmt: skip
        assert effects.append_event(root, spec)
    code, out, err = cli("check")
    assert code == 0, err + out
    assert [e["actor"] for e in journal_of(root)[-4:]] == ["хук"] * 4
