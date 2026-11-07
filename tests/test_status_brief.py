"""`strateg status --brief`: сводка «где остановились», «прервано» и «изменено вне агента»."""

from __future__ import annotations

import json
import shutil

from helpers import FIXTURE, fact, journal_of, put_frame, tree_hashes, write_claims
from strateg.project_check import check_project


def brief(cli, *extra: str) -> list[str]:
    code, out, err = cli("status", "--brief", *extra)
    assert code == 0, err
    return out.splitlines()


def kinds(root) -> list[str]:
    return [e["kind"] for e in journal_of(root)]


# --- сводка ---


def test_brief_is_short_and_says_where_we_stopped(cli, home):
    cli("new", "demo")
    lines = brief(cli)
    assert 3 <= len(lines) <= 10
    text = "\n".join(lines)
    assert "Проект: demo" in text and "режим: автопилот" in text
    assert "Шаг: 1 (Постановка)" in text
    assert "Ворота: ответов не ждут" in text
    assert "Дальше: Принять запрос" in text
    assert "Файлы выхода: нет" in text


def test_brief_fits_ten_lines_in_the_worst_case(cli, home):
    cli("new", "demo")
    cli("step", "start", "2б")
    put_frame(cli)
    cli("gate", "open", "В4", "--questions", "1")
    (home / "demo" / "claims.yaml").write_bytes((home / "demo" / "claims.yaml").read_bytes() + b"# x\n")
    assert len(brief(cli)) <= 10


def test_brief_names_waiting_gates_and_output_files(cli, home):
    cli("new", "demo")
    put_frame(cli)
    cli("gate", "open", "В1", "--questions", "2")
    text = "\n".join(brief(cli))
    assert "Ворота ждут ответа: В1" in text
    assert "ждём ответа на В1" in text
    assert "Файлы выхода: frame (черновик)" in text
    cli("gate", "answer", "В1", "--answer", "ок")
    put_frame(cli, deadline={"value": "к пятнице", "origin": "ответ человека"})
    assert "Файлы выхода: frame (версия 2)" in "\n".join(brief(cli))


def test_brief_json(cli, home):
    cli("new", "demo")
    put_frame(cli)
    data = json.loads(cli("status", "--brief", "--json")[1])
    assert data["project"] == "demo" and data["step"] == "1" and data["outputs"] == ["frame"]
    assert data["frame_draft"] is True and data["interrupted"] == [] and data["changed_outside"] == []


def test_brief_and_line_exclude_each_other(cli, home):
    cli("new", "demo")
    assert cli("status", "--brief", "--line")[0] == 2


def test_brief_on_the_example(cli, home, clock):
    clock.advance(24 * 3600)
    shutil.copytree(FIXTURE, home / "example")
    out = brief(cli, "--project", "example")
    text = "\n".join(out)
    assert "Шаг: 2б (Сбор)" in text
    assert "frame (версия 2)" in text and "decisions" in text


# --- «прервано» ---


def test_unfinished_step_is_marked_interrupted_once(cli, home):
    cli("new", "demo")
    root = home / "demo"
    cli("step", "start", "2б")
    before = len(journal_of(root))
    text = "\n".join(brief(cli))
    events = journal_of(root)
    assert len(events) == before + 1
    e = events[-1]
    assert e["kind"] == "действие" and e["status"] == "прервано" and e["name"] == "шаг 2б"
    assert "Прервано" in text and "strateg step start 2б" in text
    # второй вызов события не пишет
    brief(cli)
    brief(cli)
    assert len(journal_of(root)) == before + 1
    assert check_project(root) == []
    # шаг начинают заново, как говорит сводка; следующая сводка ничего не отмечает
    assert cli("step", "start", "2б")[0] == 0
    assert json.loads(cli("status", "--json")[1])["step_in_progress"] is True
    assert cli("step", "done", "2б")[0] == 0
    assert "Прервано" not in "\n".join(brief(cli))


def test_finished_step_is_not_interrupted(cli, home):
    cli("new", "demo")
    cli("step", "start", "1")
    cli("step", "done", "1")
    before = len(journal_of(home / "demo"))
    brief(cli)
    assert len(journal_of(home / "demo")) == before


def test_plain_status_does_not_interrupt_a_running_step(cli, home):
    """Обычный `status` читает: идущий шаг в рабочем сеансе «прерванным» не становится."""
    cli("new", "demo")
    cli("step", "start", "2б")
    before = tree_hashes(home / "demo")
    cli("status")
    cli("status", "--line")
    assert tree_hashes(home / "demo") == before


def test_unfinished_fetch_is_marked_interrupted(started, cli):
    """Не только шаги: любое действие с началом и без отметки выполнения."""
    from strateg import project as proj

    p_events = journal_of(started)
    h = proj.key_hash("fetch", "https://example.com/x", None)
    root_journal = started / "journal.jsonl"
    p = {
        "schema_version": 1,
        "time": p_events[-1]["time"],
        "actor": "CLI",
        "kind": "действие",
        "step": "1",
        "name": "strateg fetch",
        "idempotency_hash": h,
        "status": "начато",
    }
    root_journal.write_bytes(root_journal.read_bytes() + (json.dumps(p, ensure_ascii=False) + "\n").encode())
    text = "\n".join(brief(cli))
    assert "Прервано" in text and "strateg fetch" in text
    # журнал дописан руками, поэтому рядом записано и «изменено вне агента» для journal.jsonl
    last = [e for e in journal_of(started) if e["kind"] == "действие"][-1]
    assert last["status"] == "прервано" and last["idempotency_hash"] == h
    count = len(journal_of(started))
    brief(cli)
    assert len(journal_of(started)) == count


# --- «изменено вне агента» ---


def test_file_changed_outside_is_reported_once_per_change(started, cli):
    claims = started / "claims.yaml"
    before = len(journal_of(started))
    assert not any("Изменено вне агента" in line for line in brief(cli))
    assert len(journal_of(started)) == before  # правок не было: событий нет

    claims.write_bytes(claims.read_bytes() + "# правка руками\n".encode())
    lines = brief(cli)
    assert any(line.startswith("Изменено вне агента: ") and "claims.yaml" in line for line in lines)
    e = journal_of(started)[-1]
    assert e["kind"] == "изменено вне агента" and e["path"] == "claims.yaml" and e["actor"] == "CLI"
    assert len(journal_of(started)) == before + 1

    brief(cli)
    brief(cli)
    assert len(journal_of(started)) == before + 1  # одно событие на одно изменение

    claims.write_bytes(claims.read_bytes() + "# и ещё раз\n".encode())
    brief(cli)
    assert kinds(started).count("изменено вне агента") == 2
    assert check_project(started) == []


def test_output_file_edited_by_hand_is_named(cli, home):
    cli("new", "demo")
    put_frame(cli)
    frame = home / "demo" / "frame.yaml"
    frame.write_bytes(frame.read_bytes().replace("Москва".encode(), "Казань".encode()))
    assert "frame.yaml" in "\n".join(brief(cli))
    assert journal_of(home / "demo")[-1]["path"] == "frame.yaml"


def test_snapshot_edited_outside_is_named(started, cli):
    snapshot = started / "sources" / "i-1.txt"
    snapshot.write_bytes(snapshot.read_bytes() + "\nдобавлено\n".encode())
    assert "sources/i-1.txt" in "\n".join(brief(cli))


def test_files_written_by_commands_are_not_outside_changes(cli, home, tmp_path):
    cli("new", "demo")
    put_frame(cli)
    cli("gate", "open", "В1", "--questions", "1")
    cli("gate", "answer", "В1", "--answer", "ок")
    put_frame(cli, deadline={"value": "позже", "origin": "ответ человека"})
    cli("step", "start", "2б")
    cli("step", "done", "2б")
    assert not any("Изменено вне агента" in line for line in brief(cli))
    assert "изменено вне агента" not in kinds(home / "demo")


def test_deleted_file_is_reported(started, cli, tmp_path):
    assert cli("claim", "add", "--file", write_claims(tmp_path, fact()))[0] == 0
    (started / "sources" / "i-1.txt").unlink()
    text = "\n".join(brief(cli))
    assert "sources/i-1.txt" in text
    assert "файл удалён" in journal_of(started)[-1]["message"]
