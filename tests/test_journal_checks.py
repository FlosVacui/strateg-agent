"""Сверки `check_project`: журнал и реестр источников, состояние, ворота, время событий.

Каждая сверка проверяется на испорченной копии примера; сам пример остаётся чистым.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest
import yaml

from helpers import FIXTURE
from strateg.project_check import check_project


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "p"
    shutil.copytree(FIXTURE, dst)
    assert check_project(dst) == []
    return dst


def events(root) -> list[dict]:
    return [json.loads(line) for line in (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


def save_events(root, rows: list[dict]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (root / "journal.jsonl").write_bytes(text.encode("utf-8"))


def edit_yaml(root, name: str, change) -> None:
    path = root / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"))


def errors_with(root, needle: str) -> list[str]:
    return [e for e in check_project(root) if needle in e]


def event(kind: str, time: str, **extra) -> dict:
    return {"schema_version": 1, "time": time, "actor": "CLI", "kind": kind, "step": "2б", **extra}


def action(name: str, status: str, time: str, step: str = "2б") -> dict:
    key = hashlib.sha256(f"{name}|{status}|{time}".encode()).hexdigest()
    row = event("действие", time, name=name, idempotency_hash=key, status=status)
    row["step"] = step
    if status == "выполнено":
        row["duration_s"] = 1.0
    return row


# --- инструкция в источнике и пометка «подозрительный» ---


def test_instruction_event_needs_a_suspicious_source(project):
    rows = events(project)
    rows.append(
        event("найдена инструкция в источнике", "2026-11-06T10:15:00+03:00", source="И-1", message="x")
    )
    save_events(project, rows)
    found = errors_with(project, "найдена инструкция в источнике И-1")
    assert found and "не помечен «подозрительный»" in found[0]


def test_suspicious_source_needs_an_instruction_event(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0].update(suspicious=True))
    found = errors_with(project, "события «найдена инструкция в источнике» в журнале нет")
    assert found and "И-1" in found[0]


def test_suspicious_source_with_event_is_fine(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0].update(suspicious=True))
    rows = events(project)
    rows.append(
        event("найдена инструкция в источнике", "2026-11-06T10:15:00+03:00", source="И-1", message="x")
    )
    save_events(project, rows)
    assert not errors_with(project, "инструкци")


# --- шаг в state.yaml и журнал ---


def test_step_in_state_follows_the_last_started_or_done_step(project):
    rows = events(project)
    rows.append(action("шаг 2а", "начато", "2026-11-06T10:15:00+03:00", step="2а"))
    save_events(project, rows)
    found = errors_with(project, "последний начатый или выполненный шаг")
    assert found and "«2б»" in found[0] and "«2а»" in found[0]


def test_step_in_state_may_match_the_journal(project):
    rows = events(project)
    rows.append(action("шаг 2а", "выполнено", "2026-11-06T10:15:00+03:00", step="2а"))
    rows.append(action("шаг 2б", "начато", "2026-11-06T10:16:00+03:00"))
    save_events(project, rows)
    assert not errors_with(project, "последний начатый")


def test_interrupted_and_failed_marks_do_not_move_the_step(project):
    rows = events(project)
    rows.append(action("шаг 2б", "начато", "2026-11-06T10:15:00+03:00"))
    rows.append(action("шаг 2б", "прервано", "2026-11-06T10:16:00+03:00"))
    rows.append(action("шаг 3", "ошибка", "2026-11-06T10:17:00+03:00", step="3"))
    save_events(project, rows)
    assert not errors_with(project, "последний начатый")


# --- статусы ворот и события ворот ---


@pytest.mark.parametrize(
    ("gate", "wrong", "needle"),
    [
        ("В1", "впереди", "ворота В1 — «отвечено»"),
        ("В2", "ждут ответа", "ворота В2 — «пройдены автоматически»"),
        ("В3", "отвечено", "ворота В3 — «впереди»"),
        ("В4", "пройдены автоматически", "ворота В4 — «впереди»"),
    ],
)
def test_gate_status_follows_gate_events(project, gate, wrong, needle):
    edit_yaml(project, "state.yaml", lambda d: d["gates"].update({gate: wrong}))
    found = errors_with(project, f"поле gates.{gate}")
    assert found and needle in found[0]


def test_open_gate_without_answer_is_waiting(project):
    rows = events(project)
    rows.append(
        event(
            "ворота",
            "2026-11-06T10:15:00+03:00",
            gate={
                "gate": "В3",
                "mode": "автопилот",
                "questions": 1,
                "opened_at": "2026-11-06T10:15:00+03:00",
            },
        )
    )
    save_events(project, rows)
    assert errors_with(project, "поле gates.В3")  # в state.yaml В3 всё ещё «впереди»
    edit_yaml(project, "state.yaml", lambda d: d["gates"].update({"В3": "ждут ответа"}))
    assert not errors_with(project, "поле gates")


# --- время событий не убывает ---


def test_event_time_does_not_go_back(project):
    rows = events(project)
    rows[-1]["time"] = "2026-11-06T10:00:30+03:00"
    save_events(project, rows)
    found = errors_with(project, "время события раньше предыдущей строки")
    assert found and f"строка {len(rows)}" in found[0]


def test_equal_event_times_are_fine(project):
    rows = events(project)
    rows[-1]["time"] = rows[-2]["time"]
    save_events(project, rows)
    assert not errors_with(project, "время события")


def test_time_is_compared_with_the_zone(project):
    rows = events(project)
    # по записи «08:00» меньше «10:14», но 08:00 UTC позже 10:14:05+03:00: сравниваются моменты времени
    rows[-1]["time"] = "2026-11-06T08:00:00+00:00"
    save_events(project, rows)
    assert not errors_with(project, "время события")
