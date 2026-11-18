"""`strateg new <id> --from <проект>`: копия проекта-примера, с которой продолжают работу (С-12, ADR-0003).

Фикстуры `tests/fixtures/project_*` кладутся в папку проектов под именем `example`, как лежит пример
в репозитории. Источник не меняется ни на байт; копия — обычный проект: проходит `check`, продолжается
командами, а время и стоимость считаются от неё.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from helpers import PUBLISHER, edit_text, journal_of, load_yaml, write_claims
from strateg import project as proj
from strateg import project_copy
from strateg.project_check import check_project

FIXTURES = Path(__file__).parent / "fixtures"
NAMES = ("project_valid", "project_complete", "project_follow_up")
MSK = dt.timezone(dt.timedelta(hours=3))


def put_example(home: Path, fixture: str = "project_follow_up", name: str = "example") -> Path:
    dest = home / name
    shutil.copytree(FIXTURES / fixture, dest)
    return dest


def snapshot(root: Path) -> dict[str, str | None]:
    """Все файлы и папки: хеш файла или None у папки. `.tmp/lock` тоже в счёт: источник его не получает."""
    out: dict[str, str | None] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def state_of(root: Path) -> dict:
    return load_yaml(root / "state.yaml")


def new_copy(cli, *extra: str, name: str = "mine", source: str = "example"):
    return cli("new", name, "--from", source, *extra)


@pytest.fixture
def example(home) -> Path:
    return put_example(home)


# --- копия работает как проект ---


@pytest.mark.parametrize("fixture", NAMES)
def test_copy_passes_check_and_status_shows_step_and_next(cli, home, fixture):
    src = put_example(home, fixture)
    step = state_of(src)["step"]
    code, out, err = new_copy(cli)
    assert code == 0, err
    assert "копия «example»" in out and "выбран активным" in out
    assert (home / ".active").read_text(encoding="utf-8").strip() == "mine"
    assert check_project(home / "mine") == []
    code, out, err = cli("check")
    assert code == 0, err
    code, out, err = cli("status")
    assert code == 0, err
    assert out.startswith("Проект: mine") and f"Шаг: {step} " in out
    assert "Дальше: " in out
    assert "Изменено вне агента" not in out


@pytest.mark.parametrize("fixture", NAMES)
def test_copy_has_its_own_number_and_clean_hashes(cli, home, fixture):
    put_example(home, fixture)
    assert new_copy(cli)[0] == 0
    root = home / "mine"
    state = state_of(root)
    assert state["id"] == "mine"
    for rel, recorded in state["files"].items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == recorded, rel
    assert not [e for e in journal_of(root) if e["kind"] == "изменено вне агента"]
    before = (root / "journal.jsonl").read_bytes()
    assert cli("status")[0] == 0 and cli("check")[0] == 0
    assert (
        root / "journal.jsonl"
    ).read_bytes() == before  # повторный status ничего не пишет и не находит правок


def test_json_output(cli, example):
    code, out, err = new_copy(cli, "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["ok"] is True and data["project"] == "mine" and data["from"] == "example"
    assert data["active"] is True and data["step"] == "7" and data["files"] > 10
    assert data["not_copied"] == [".tmp/", ".cache/"] and data["next_action"]


def test_source_may_be_a_folder_path(cli, tmp_path, home):
    elsewhere = tmp_path / "чужая папка" / "пример проекта"
    shutil.copytree(FIXTURES / "project_valid", elsewhere)
    before = snapshot(elsewhere)
    code, out, err = new_copy(cli, source=str(elsewhere))
    assert code == 0, err
    assert snapshot(elsewhere) == before
    assert check_project(home / "mine") == []


def test_cyrillic_and_spaces_in_paths(cli, tmp_path, monkeypatch):
    folder = tmp_path / "мои проекты 2026" / "для жюри"
    monkeypatch.setenv(proj.PROJECTS_ENV, str(folder))
    src = put_example(folder, "project_complete", "образец")  # папка с кириллицей в имени
    before = snapshot(src)
    assert cli("new", "mine", "--from", str(src))[0] == 0
    assert snapshot(src) == before
    assert check_project(folder / "mine") == []
    assert cli("status")[0] == 0


# --- источник не меняется ---


@pytest.mark.parametrize("fixture", NAMES)
def test_source_is_not_changed_by_a_byte(cli, home, fixture):
    src = put_example(home, fixture)
    before = snapshot(src)
    assert ".tmp/lock" not in before
    assert new_copy(cli)[0] == 0
    assert snapshot(src) == before  # ни файла, ни папки, в том числе `.tmp/lock`
    assert new_copy(cli, name="second")[0] == 0
    assert cli("step", "start", "7")[0] == 0  # работа идёт в копии: активна вторая
    assert snapshot(src) == before


def test_source_with_service_files_is_read_without_touching_them(cli, example):
    (example / ".tmp").mkdir()
    (example / ".tmp" / "lock").write_bytes(b"\n")
    (example / ".tmp" / "pending").mkdir()
    (example / ".tmp" / "pending" / "И-9.txt").write_text("до подтверждения", encoding="utf-8")
    before = snapshot(example)
    assert new_copy(cli)[0] == 0
    assert snapshot(example) == before


def test_service_files_are_not_carried_over(cli, home, example):
    (example / ".tmp" / "pending").mkdir(parents=True)
    (example / ".tmp" / "pending" / "И-9.txt").write_text("исходник с ПДн", encoding="utf-8")
    (example / ".tmp" / "cost").mkdir()
    (example / ".tmp" / "cost" / "offsets.json").write_text("{}", encoding="utf-8")
    (example / ".cache").mkdir()
    (example / ".cache" / "fetch.json").write_text("{}", encoding="utf-8")
    (example / "Thumbs.db").write_bytes(b"x")
    assert new_copy(cli)[0] == 0
    left = {p.relative_to(home / "mine").as_posix() for p in (home / "mine").rglob("*")}
    assert not [p for p in left if p.startswith((".cache", "Thumbs"))], left
    assert not [p for p in left if p.startswith(".tmp/") and p != ".tmp/lock"], left
    assert (home / "mine" / "inputs" / "report.txt").is_file() and (home / "mine" / "history").is_dir()


def test_empty_folders_are_created_because_git_does_not_keep_them(cli, home, example):
    shutil.rmtree(example / "outputs")  # в git пустых папок нет
    assert new_copy(cli)[0] == 0
    for name in ("inputs", "sources", "outputs", ".tmp"):
        assert (home / "mine" / name).is_dir(), name


# --- журнал, время и стоимость ---


def test_journal_keeps_old_events_and_gets_a_copy_event(cli, home, example):
    old = (example / "journal.jsonl").read_bytes()
    assert new_copy(cli)[0] == 0
    new = (home / "mine" / "journal.jsonl").read_bytes()
    assert new.startswith(old)  # прежние события на месте, как были
    events = journal_of(home / "mine")
    last = events[-1]
    assert last["actor"] == "CLI" and last["name"] == "копия проекта" and last["status"] == "выполнено"
    assert last["step"] == "7"
    for needle in (
        "example",
        "шаг 7 (Выпуск)",
        "В1 отвечено",
        "В5 впереди",
        "$0,81",
        "в учёт копии не входит",
    ):
        assert needle in last["message"], needle
    assert dt.datetime.fromisoformat(last["time"]) >= dt.datetime.fromisoformat(events[-2]["time"])
    assert str(home) not in last["message"]  # личных путей в журнале нет


def test_cost_starts_from_zero_and_the_budget_is_not_charged_with_the_example(cli, home, example):
    assert state_of(example)["budget"]["spent_usd"] == 0.81
    assert new_copy(cli)[0] == 0
    root = home / "mine"
    assert (root / "cost.jsonl").read_bytes() == b""  # деньги, потраченные на пример, остались в нём
    budget = state_of(root)["budget"]
    assert "spent_usd" not in budget and budget["spent_minutes"] == 0
    data = json.loads(cli("status", "--json")[1])
    assert data["cost_usd"] is None and data["budget"]["spent_usd"] is None
    assert data["budget"]["spent_minutes"] < 0.1


def test_time_of_the_copy_counts_from_the_copy(cli, home, example, clock):
    clock.value = dt.datetime(2026, 11, 18, 12, 0, tzinfo=MSK)  # пример остановлен 12 днями раньше
    assert new_copy(cli)[0] == 0
    clock.advance(300)
    data = json.loads(cli("status", "--json")[1])
    assert data["work_minutes"] == pytest.approx(5.0)  # шаг 7 начат в примере, считается от копии
    code, out, err = cli("step", "done", "7", "--next", "Готово")
    assert code == 0, err
    assert json.loads(cli("step", "done", "7", "--json")[1])["already_done"] is True
    assert json.loads(cli("status", "--json")[1])["clock_minutes"] == pytest.approx(
        5.0
    )  # по часам тоже от копии
    done = [e for e in journal_of(home / "mine") if e.get("name") == "шаг 7" and e["status"] == "выполнено"][
        -1
    ]
    assert done["duration_s"] == pytest.approx(300.0)
    assert state_of(home / "mine")["budget"]["spent_minutes"] == pytest.approx(5.0)
    assert cli("check")[0] == 0


def test_restarted_step_is_charged_only_from_the_copy(cli, home, example, clock):
    clock.value = dt.datetime(2026, 11, 18, 12, 0, tzinfo=MSK)
    assert new_copy(cli)[0] == 0
    clock.advance(120)
    assert cli("step", "start", "7")[0] == 0  # прежняя попытка из примера записывается как «прервано»
    clock.advance(600)
    assert cli("step", "done", "7")[0] == 0
    events = journal_of(home / "mine")
    interrupted = [e for e in events if e.get("status") == "прервано"][-1]
    assert interrupted["duration_s"] == pytest.approx(0.0)
    done = [e for e in events if e.get("name") == "шаг 7" and e["status"] == "выполнено"][-1]
    assert done["duration_s"] == pytest.approx(600.0)
    assert json.loads(cli("status", "--json")[1])["work_minutes"] == pytest.approx(10.0)


def test_a_copy_of_a_copy_counts_from_its_own_copy_event(cli, home, example, clock):
    assert new_copy(cli)[0] == 0
    clock.advance(7200)
    assert cli("step", "start", "7")[0] == 0
    clock.advance(3600)
    assert cli("step", "done", "7")[0] == 0
    clock.advance(86400)
    assert new_copy(cli, name="third", source="mine")[0] == 0
    assert cli("check")[0] == 0
    events = [e for e in journal_of(home / "third") if e.get("name") == "копия проекта"]
    assert len(events) == 2 and "«mine»" in events[-1]["message"]
    data = json.loads(cli("status", "--json")[1])
    assert data["work_minutes"] == pytest.approx(0.0) and data["cost_usd"] is None


def test_source_gaps_found_before_copying_are_noted_once(cli, home, example):
    (example / "gaps.yaml").write_text(
        (example / "gaps.yaml").read_text(encoding="utf-8") + "# правка руками\n",
        encoding="utf-8",
        newline="\n",
    )
    assert check_project(example) == []
    before = snapshot(example)
    assert new_copy(cli)[0] == 0
    assert snapshot(example) == before
    root = home / "mine"
    notes = [e for e in journal_of(root) if e["kind"] == "изменено вне агента"]
    assert [e["path"] for e in notes] == ["gaps.yaml"]
    assert journal_of(root)[-1]["name"] == "копия проекта"
    assert cli("status")[0] == 0
    assert len([e for e in journal_of(root) if e["kind"] == "изменено вне агента"]) == 1  # больше не пишется


def test_mentions_of_the_source_folder_are_reported_not_rewritten(cli, home, example):
    (example / "inputs" / "note.txt").write_text(
        "см. projects/example/outputs/situation.md", encoding="utf-8"
    )
    code, out, err = new_copy(cli, "--json")
    assert code == 0, err
    assert json.loads(out)["mentions"] == ["inputs/note.txt"]
    assert "projects/example/" in (home / "mine" / "inputs" / "note.txt").read_text(encoding="utf-8")


# --- команды после копии ---


@pytest.mark.parametrize("fixture", NAMES)
def test_mutating_commands_work_on_the_copy(cli, home, tmp_path, clock, fixture):
    src = put_example(home, fixture)
    before = snapshot(src)
    clock.value = dt.datetime(2026, 11, 18, 12, 0, tzinfo=MSK)
    assert new_copy(cli)[0] == 0
    step = state_of(home / "mine")["step"]
    assert cli("step", "start", step)[0] == 0
    quote = "Первые семь дней бесплатно"
    claim = {
        "text": "Первые семь дней подписка «Защита» бесплатна",
        "type": "факт",
        "subject": "пробный период подписки «Защита»",
        "period": "2026",
        "region": "RU-MOW",
        "quotes": [{"source": "И-1", "text": quote, **PUBLISHER}],
    }
    code, out, err = cli("claim", "add", "--file", write_claims(tmp_path, claim))
    assert code == 0, err
    clock.advance(90)
    assert cli("step", "done", step)[0] == 0
    assert cli("check")[0] == 0
    root = home / "mine"
    assert not [e for e in journal_of(root) if e["kind"] == "изменено вне агента"]
    assert state_of(root)["budget"]["spent_minutes"] > 0
    assert snapshot(src) == before


def test_fetch_on_the_copy_writes_its_own_cache(cli, home, example, net, page_html):
    net.add("https://example.com/protection", page_html)
    assert new_copy(cli)[0] == 0
    before = snapshot(example)
    code, out, err = cli("fetch", "https://example.com/protection", "--region", "RU-MOW")
    assert code == 0, err
    assert (home / "mine" / ".cache" / "fetch.json").is_file()
    assert snapshot(example) == before
    assert cli("check")[0] == 0


# --- отказы ---


def test_refuses_a_taken_number(cli, home, example):
    taken = put_example(home, "project_valid", "mine")
    before = snapshot(taken)
    code, out, err = new_copy(cli)
    assert code == 1 and "проект mine уже существует" in err
    assert snapshot(taken) == before
    assert not (home / ".active").exists()


def test_refuses_to_copy_into_the_source_itself(cli, home, example):
    before = snapshot(example)
    code, out, err = new_copy(cli, name="example")
    assert code == 1 and "уже существует" in err
    assert snapshot(example) == before


def test_refuses_when_there_is_no_source(cli, home):
    home.mkdir()
    code, out, err = new_copy(cli, source="example")
    assert code == 1 and "проект-источник не найден: example" in err and "projects/example/" in err
    assert not (home / "mine").exists() and not (home / ".active").exists()
    code, out, err = new_copy(cli, source=str(home / "нет такой папки"))
    assert code == 1 and "не найден" in err
    assert not (home / "mine").exists()


def test_refuses_a_source_that_fails_check(cli, home, example):
    edit_text(example, "claims.yaml", "type: факт", "type: выдумка")
    errors = check_project(example)
    assert errors
    before = snapshot(example)
    code, out, err = new_copy(cli)
    assert code == 1 and "не проходит проверку" in err
    assert errors[0] in err
    assert not (home / "mine").exists() and not (home / ".active").exists()
    assert snapshot(example) == before
    code, out, err = new_copy(cli, "--json")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False and data["details"]


def test_refuses_bad_number_and_mode_with_from(cli, home, example):
    code, out, err = new_copy(cli, name="Плохой номер")
    assert code == 2 and "не подходит" in err
    code, out, err = new_copy(cli, "--mode", "пилот")
    assert code == 2 and "--mode" in err
    code, out, err = new_copy(cli, "--region", "RU-MOW")
    assert code == 2
    assert not (home / "mine").exists()


def test_a_failure_in_the_middle_leaves_no_folder_and_no_active_project(cli, home, example, monkeypatch):
    calls = []
    real = shutil.copyfile

    def flaky(src, dst, *a, **kw):
        calls.append(dst)
        if len(calls) == 5:
            raise OSError("диск отказал")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(project_copy.shutil, "copyfile", flaky)
    before = snapshot(example)
    with pytest.raises(OSError):
        new_copy(cli)
    assert not (home / "mine").exists() and not (home / ".active").exists()
    assert snapshot(example) == before


def test_a_copy_that_fails_its_own_check_is_removed(cli, home, example, monkeypatch):
    real = project_copy.check_project
    calls = []

    def broken(root):
        calls.append(root)
        return real(root) if Path(root) == example else ["файл повреждён при копировании"]

    monkeypatch.setattr(project_copy, "check_project", broken)
    code, out, err = new_copy(cli)
    assert code == 1 and "файл повреждён при копировании" in err
    assert not (home / "mine").exists() and not (home / ".active").exists()


def test_plain_new_is_unchanged(cli, home):
    code, out, err = cli("new", "plain", "--mode", "пилот")
    assert code == 0 and "режим: пилот" in out
    assert state_of(home / "plain")["mode"] == "пилот"
    code, out, err = cli("new", "other")
    assert code == 0 and state_of(home / "other")["mode"] == "автопилот"
    assert not [e for e in journal_of(home / "other") if e.get("name") == "копия проекта"]


# --- вход «продолжи пример» в скилле ---


def test_router_skill_has_the_continue_example_entry():
    text = (Path(__file__).parent.parent / ".claude" / "skills" / "strateg" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "«Продолжи пример»" in text
    assert "uv run strateg new <id> --from example" in text
    entry = next(line for line in text.splitlines() if "Продолжи пример" in line)
    assert "status" in entry and "Дальше" in entry and "projects/example/" in entry
