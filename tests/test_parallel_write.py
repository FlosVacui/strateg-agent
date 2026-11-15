"""Параллельные команды пишут в один проект и не теряют записей друг друга (ADR-0002, «Параллельные команды»).

Настоящие процессы: `tests/race_cli.py` запускает CLI с загрузчиком-заглушкой и паузой перед записью
реестров. Пауза делает гонку воспроизводимой: без блокировки на всё «прочитать — изменить — записать»
соседи успевают прочитать одно и то же состояние, и тест падает, а не проходит случайно. Сеть не используется.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from helpers import fact, journal_of, load_yaml
from strateg import project as proj
from strateg import storage
from strateg.hooks import effects, rules
from strateg.project_check import check_project

ROOT = Path(__file__).resolve().parent.parent
RACE = Path(__file__).resolve().parent / "race_cli.py"
PID = "race"


def child_env(projects: Path, **extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("STRATEG_", "CLAUDE_", "RACE_"))}
    return {
        **base,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "STRATEG_PROJECTS_DIR": str(projects),
        "STRATEG_HOOKS": "0",
        **extra,
    }


def spawn(projects: Path, *argv: str, **env: str) -> subprocess.Popen:
    """Процесс `strateg`; после открытия проекта команда ждёт 0,3 с: соседи успевают записать."""
    env.setdefault("RACE_OPEN_DELAY", "0.3" if argv[0] in ("add", "claim", "fetch") else "0")
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", str(RACE), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(projects, **env),
        cwd=ROOT,
    )


def finish(procs: list[subprocess.Popen]) -> list[tuple[int, dict | None, str]]:
    """Код возврата, JSON ответа и текст ошибок каждого процесса."""
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        text = stdout.decode("utf-8", "replace").strip()
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        out.append((p.returncode, data, stderr.decode("utf-8", "replace")))
    return out


def run(projects: Path, *argv: str, **env: str) -> tuple[int, dict | None, str]:
    return finish([spawn(projects, *argv, **env)])[0]


@pytest.fixture
def projects(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def race(projects) -> Path:
    assert run(projects, "new", PID)[0] == 0
    return projects / PID


def page(i: int) -> str:
    return (
        f"<html><head><title>Тариф {i}</title></head><body><main><article>"
        f"<p>Подписка «Защита» номер {i} в Москве стоит 199 рублей в месяц.</p>"
        f"<p>Первые семь дней бесплатно, дальше списывается абонентская плата по тарифу {i}.</p>"
        "</article></main></body></html>"
    )


def url(i: int) -> str:
    return f"https://example.com/tariff-{i}"


def quote(i: int) -> str:
    return f"Подписка «Защита» номер {i} в Москве стоит 199 рублей в месяц"


@pytest.fixture
def pages(tmp_path) -> Path:
    path = tmp_path / "pages.json"
    path.write_text(json.dumps({url(i): page(i) for i in range(1, 9)}, ensure_ascii=False), encoding="utf-8")
    return path


def sources_of(root: Path) -> list[dict]:
    return load_yaml(root / "sources.yaml")["sources"]


def claims_of(root: Path) -> list[dict]:
    return load_yaml(root / "claims.yaml")["claims"]


def done_keys(root: Path, name: str) -> list[str]:
    return [
        e["idempotency_hash"]
        for e in journal_of(root)
        if e.get("name") == name and e.get("status") == "выполнено"
    ]


def assert_clean(root: Path) -> None:
    """Ни ошибок проверки, ни ложного «изменено вне агента»: реестры, журнал и хеши в state.yaml сходятся."""
    assert check_project(root) == []
    assert "изменено вне агента" not in [e["kind"] for e in journal_of(root)]
    assert proj.Project.load(root).outside == []


def fetch_all(projects: Path, pages: Path, numbers: range, **env: str) -> None:
    for i in numbers:
        code, data, err = run(
            projects, "fetch", url(i), "--project", PID, "--json", RACE_PAGES=str(pages), **env
        )
        assert code == 0, err


def claim_file(tmp_path: Path, i: int, source: int | None = None, subject: int | None = None) -> str:
    """Файл с фактом `i`; цитата из источника `source` (по умолчанию И-i), предмет — вариант `subject`."""
    n = source or i
    item = fact(
        quote(n),
        text=f"Подписка «Защита» номер {n} в Москве стоит 199 рублей в месяц",
        subject=f"цена подписки, вариант {subject or i}",
    )
    item["quotes"][0]["source"] = f"И-{n}"
    path = tmp_path / f"claim-{i}.yaml"
    path.write_text(yaml.safe_dump(item, allow_unicode=True), encoding="utf-8")
    return str(path)


# --- параллельные add ---------------------------


def test_parallel_add_of_different_files_gives_different_numbers_and_loses_nothing(race, projects, tmp_path):
    files = []
    for k in range(1, 7):
        f = race / "inputs" / f"заметка-{k}.txt"
        f.write_text(
            f"Заметка номер {k}: клиенты просят защиту детей, её цена {100 + k} рублей.", encoding="utf-8"
        )
        files.append(f)
    procs = [
        spawn(projects, "add", str(f), "--project", PID, "--json", RACE_WRITE_DELAY="0.3") for f in files
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    numbers = sorted(data["source"] for _, data, _ in results)
    assert numbers == sorted(f"И-{n}" for n in range(1, 7))  # номера разные
    records = sources_of(race)
    assert sorted(s["id"] for s in records) == numbers
    assert {s["path"] for s in records} == {f"inputs/{f.name}" for f in files}  # ни одна запись не потеряна
    for s in records:  # у каждой свой текст, и он лежит на месте
        assert (race / s["text_path"]).is_file()
    assert len(set(done_keys(race, "strateg add"))) == 6
    assert_clean(race)


def test_parallel_add_of_the_same_file_gives_one_number(race, projects):
    f = race / "inputs" / "заметка.txt"
    f.write_text("Одна и та же заметка про защиту детей, цена 199 рублей.", encoding="utf-8")
    procs = [
        spawn(projects, "add", str(f), "--project", PID, "--json", RACE_WRITE_DELAY="0.3") for _ in range(4)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert {data["source"] for _, data, _ in results} == {"И-1"}
    assert sorted(data["duplicate"] for _, data, _ in results) == [False, True, True, True]
    assert len(sources_of(race)) == 1
    assert len(done_keys(race, "strateg add")) == 1
    assert_clean(race)


# --- параллельные fetch ---------------------------


def test_parallel_fetch_of_different_addresses_keeps_every_source_and_one_done_per_key(race, projects, pages):
    procs = [
        spawn(
            projects,
            "fetch",
            url(i),
            "--project",
            PID,
            "--json",
            RACE_PAGES=str(pages),
            RACE_NET_DELAY="0.2",
            RACE_WRITE_DELAY="0.2",
        )
        for i in range(1, 6)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    numbers = sorted(data["source"] for _, data, _ in results)
    assert numbers == [f"И-{n}" for n in range(1, 6)]
    records = {s["url"]: s for s in sources_of(race)}
    assert set(records) == {url(i) for i in range(1, 6)}
    for i in range(1, 6):  # снимок источника — его собственный текст, а не чужой
        text = (race / records[url(i)]["text_path"]).read_text(encoding="utf-8")
        assert f"номер {i} " in text
    assert sorted(p.name for p in (race / "sources").iterdir()) == [f"i-{n}.txt" for n in range(1, 6)]
    keys = done_keys(race, "strateg fetch")
    assert len(keys) == 5 and len(set(keys)) == 5  # повторных «выполнено» с тем же ключом нет
    assert_clean(race)


def test_parallel_fetch_of_the_same_address_gives_one_source_one_snapshot_one_done(race, projects, pages):
    procs = [
        spawn(
            projects,
            "fetch",
            url(1),
            "--project",
            PID,
            "--json",
            RACE_PAGES=str(pages),
            RACE_NET_DELAY="0.4",
            RACE_WRITE_DELAY="0.2",
        )
        for _ in range(4)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert {data["source"] for _, data, _ in results} == {"И-1"}  # все получили прежний номер
    assert len(sources_of(race)) == 1
    assert [p.name for p in (race / "sources").iterdir()] == ["i-1.txt"]  # лишнего снимка нет
    assert len(done_keys(race, "strateg fetch")) == 1  # одна «выполнено» с записью
    statuses = [e["status"] for e in journal_of(race) if e.get("name") == "strateg fetch"]
    assert statuses.count("выполнено") == 1 and statuses.count("пропущено") == 3
    assert_clean(race)


def test_parallel_refresh_of_the_same_address_records_one_recheck(race, projects, pages):
    fetch_all(projects, pages, range(1, 2))
    procs = [
        spawn(
            projects,
            "fetch",
            url(1),
            "--refresh",
            "--project",
            PID,
            "--json",
            RACE_PAGES=str(pages),
            RACE_NET_DELAY="0.4",
            RACE_WRITE_DELAY="0.2",
        )
        for _ in range(3)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert len(sources_of(race)) == 1  # текст тот же: нового источника нет
    keys = done_keys(race, "strateg fetch")
    assert len(keys) == len(set(keys)) == 2  # первая загрузка и одна перепроверка
    assert_clean(race)


# --- параллельные claim add и смешанные команды -----------------------------------------------------------


def test_parallel_claim_add_keeps_every_claim_with_its_own_number(race, projects, pages, tmp_path):
    fetch_all(projects, pages, range(1, 6))
    procs = [
        spawn(
            projects,
            "claim",
            "add",
            "--file",
            claim_file(tmp_path, i),
            "--project",
            PID,
            "--json",
            RACE_WRITE_DELAY="0.3",
        )
        for i in range(1, 6)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    ids = sorted(r["id"] for _, data, _ in results for r in data["claims"])
    assert ids == [f"У-{n}" for n in range(1, 6)]
    stored = claims_of(race)
    assert sorted(c["id"] for c in stored) == ids
    assert len({c["subject"] for c in stored}) == 5  # ни одно утверждение не затёрто чужим
    assert len(set(done_keys(race, "strateg claim add"))) == 5
    assert_clean(race)


def test_claim_add_and_fetch_at_the_same_time_keep_both_records(race, projects, pages, tmp_path):
    fetch_all(projects, pages, range(1, 3))
    procs = [
        spawn(
            projects,
            "claim",
            "add",
            "--file",
            claim_file(tmp_path, 1),
            "--project",
            PID,
            "--json",
            RACE_WRITE_DELAY="0.4",
        ),
        spawn(
            projects,
            "fetch",
            url(3),
            "--project",
            PID,
            "--json",
            RACE_PAGES=str(pages),
            RACE_WRITE_DELAY="0.4",
        ),
        spawn(
            projects,
            "claim",
            "add",
            "--file",
            claim_file(tmp_path, 2),
            "--project",
            PID,
            "--json",
            RACE_WRITE_DELAY="0.4",
        ),
        spawn(
            projects,
            "fetch",
            url(4),
            "--project",
            PID,
            "--json",
            RACE_PAGES=str(pages),
            RACE_WRITE_DELAY="0.4",
        ),
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert sorted(s["id"] for s in sources_of(race)) == ["И-1", "И-2", "И-3", "И-4"]
    assert sorted(c["id"] for c in claims_of(race)) == ["У-1", "У-2"]
    assert_clean(race)  # и хеши файлов в state.yaml не отстали: «изменено вне агента» нет


def test_parallel_claim_replace_and_revoke_do_not_overwrite_each_other(race, projects, pages, tmp_path):
    fetch_all(projects, pages, range(1, 4))
    for i in (1, 2):
        assert run(projects, "claim", "add", "--file", claim_file(tmp_path, i), "--project", PID)[0] == 0
    procs = [
        spawn(
            projects,
            "claim",
            "revoke",
            "У-1",
            "--reason",
            "устарело",
            "--project",
            PID,
            RACE_WRITE_DELAY="0.4",
        ),
        spawn(
            projects,
            "claim",
            "replace",
            "У-2",
            "--file",
            claim_file(tmp_path, 3, subject=2),  # замена: тот же предмет, другой источник
            "--project",
            PID,
            RACE_WRITE_DELAY="0.4",
        ),
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    by_id = {c["id"]: c for c in claims_of(race)}
    assert by_id["У-1"]["status"] == "отозвано"
    assert by_id["У-2"]["status"] == "заменено" and by_id["У-3"]["replaces"] == "У-2"
    assert_clean(race)


# --- механизм ---------------------------


def test_transaction_rereads_the_project_under_the_lock(race, projects):
    stale = proj.Project.load(race)  # открыт до того, как сосед записал источник
    f = race / "inputs" / "сосед.txt"
    f.write_text("Заметка соседа про защиту детей, цена 199 рублей в месяц.", encoding="utf-8")
    assert run(projects, "add", str(f), "--project", PID)[0] == 0
    assert stale.sources.sources == []
    with proj.transaction(stale):
        assert [s.id for s in stale.sources.sources] == ["И-1"]  # номер строится по этому состоянию
    assert stale.journal[-1].name == "strateg add"


def test_registers_are_not_written_without_the_lock(race):
    p = proj.Project.load(race)
    with pytest.raises(RuntimeError, match="вне блокировки"):
        p.save_sources()
    with pytest.raises(RuntimeError, match="вне блокировки"):
        p.save_claims()
    with proj.transaction(p):
        p.save_sources()
        p.save_claims()


def test_rollback_keeps_what_a_neighbour_wrote_while_the_command_held_the_folder(race, projects):
    code = (
        "import sys; from pathlib import Path\n"
        "from strateg.hooks import effects, rules\n"
        "ok = effects.append_event(Path(sys.argv[1]), rules.EventSpec('отказ хука', 'сосед', name='x'))\n"
        "print(ok, flush=True)\n"
    )
    before = journal_of(race)
    with pytest.raises(RuntimeError, match="упала"), proj.transaction(race):
        p = proj.Project.load(race)
        p.add_event(p.event(kind="ошибка", message="событие откатываемой команды"))
        neighbour = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", code, str(race)],
            stdout=subprocess.PIPE,
            env=child_env(projects, STRATEG_LOCK_TIMEOUT="20"),
        )
        with pytest.raises(subprocess.TimeoutExpired):  # папка занята: сосед ждёт и ничего не пишет
            neighbour.wait(timeout=1.5)
        assert [e for e in journal_of(race)[len(before) :] if e["kind"] == "отказ хука"] == []
        raise RuntimeError("команда упала")
    assert neighbour.stdout.readline().strip() == b"True"
    neighbour.wait(timeout=30)
    events = journal_of(race)
    assert [e["kind"] for e in events[len(before) :]] == ["отказ хука"]  # запись соседа на месте, своей нет
    assert_clean(race)


def test_command_holds_the_folder_for_its_whole_body_not_only_for_the_snapshot(race, projects):
    holder = (
        "import sys, time; from pathlib import Path\n"
        "from strateg import project as proj\n"
        "with proj.transaction(Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(2)\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", holder, str(race)],
        stdout=subprocess.PIPE,
        env=child_env(projects),
    )
    assert p.stdout.readline().strip() == b"held"
    with pytest.raises(storage.LockTimeout):
        with storage.project_lock(race, timeout=0.5):
            pass
    p.wait(timeout=30)
    assert effects.append_event(race, rules.EventSpec("отказ хука", "после", name="t"))


# --- исходник с ПДн: соседний add уже перенёс файл ---------------------------------------------------


def test_parallel_add_of_a_file_with_personal_data_gives_one_number_and_no_traceback(race, projects):
    f = race / "inputs" / "анкета.txt"
    f.write_text("Клиент: телефон +7 (916) 123-45-67, почта ivan.petrov@example.com.", encoding="utf-8")
    procs = [
        spawn(projects, "add", str(f), "--project", PID, "--json", RACE_WRITE_DELAY="0.5") for _ in range(3)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert all("Traceback" not in err for _, _, err in results)
    assert {data["source"] for _, data, _ in results} == {"И-1"}
    assert len(sources_of(race)) == 1
    pending = list((race / ".tmp" / "pending" / "И-1").iterdir())
    assert [p.name for p in pending] == ["анкета.txt"]  # исходник перенесён один раз, без копий
    assert not f.exists()


# --- параметрический тест: команды, которые меняют state.yaml, не затирают чужую правку --------------------


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "project_follow_up"

COMMANDS = {
    "gate open": ("gate", "open", "В1", "--questions", "1"),
    "step start": ("step", "start", "1"),
    "cost add": (
        "cost",
        "add",
        "--session",
        "s1",
        "--role",
        "main",
        "--model",
        "sonnet",
        "--message-id",
        "m1",
    ),
    "metric add": ("metric", "add", "ПП-5", "--value", "33 %", "--date", "2026-10-09"),
}


@pytest.mark.parametrize("name", list(COMMANDS))
def test_command_keeps_a_neighbours_change_of_state_yaml_made_after_it_opened_the_project(
    name, projects, tmp_path
):
    """Команда открыла проект и ждёт; сосед меняет лимит бюджета. Команда не должна вернуть старый лимит."""
    if name == "metric add":
        shutil.copytree(FIXTURE, projects / PID)  # нужна строка плана проверки ПП-5
    else:
        assert run(projects, "new", PID)[0] == 0
    slow = spawn(projects, *COMMANDS[name], "--project", PID, "--json", RACE_OPEN_DELAY="3")
    time.sleep(1.0)  # команда уже стартовала и вот-вот откроет проект
    neighbour = run(projects, "budget", "set", "--usd", "7", "--project", PID, RACE_OPEN_DELAY="0")
    assert neighbour[0] == 0, neighbour[2]
    code, _, err = finish([slow])[0]
    assert code == 0, err
    state = load_yaml(projects / PID / "state.yaml")
    assert state["budget"]["limit_usd"] == 7.0
    if name != "metric add":  # у фикстуры есть прежние замечания проверки
        assert check_project(projects / PID) == []


def test_three_parallel_gate_open_give_one_event(race, projects):
    procs = [
        spawn(
            projects,
            "gate",
            "open",
            "В1",
            "--questions",
            "1",
            "--project",
            PID,
            "--json",
            RACE_OPEN_DELAY="1",
        )
        for _ in range(3)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert sum(e["kind"] == "ворота" for e in journal_of(race)) == 1
    assert sum(bool(d.get("already_open")) for _, d, _ in results) == 2
    assert_clean(race)


def test_three_parallel_step_start_give_three_different_attempts(race, projects):
    procs = [
        spawn(projects, "step", "start", "1", "--project", PID, "--json", RACE_OPEN_DELAY="1")
        for _ in range(3)
    ]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    assert sorted(d["attempt"] for _, d, _ in results) == [1, 2, 3]
    assert_clean(race)


def test_three_parallel_cost_add_of_one_message_record_it_once(race, projects):
    args = ("cost", "add", "--session", "s1", "--role", "main", "--model", "sonnet", "--message-id", "m1")
    procs = [spawn(projects, *args, "--project", PID, "--json", RACE_OPEN_DELAY="1") for _ in range(3)]
    results = finish(procs)
    assert all(code == 0 for code, _, _ in results), [err for _, _, err in results]
    lines = (race / "cost.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert_clean(race)


# --- сеть не держит папку ---------------------------


def test_a_neighbour_writes_while_fetch_waits_for_the_network(race, projects, pages):
    slow = spawn(
        projects,
        "fetch",
        url(1),
        "--project",
        PID,
        "--json",
        RACE_PAGES=str(pages),
        RACE_NET_DELAY="6",
        RACE_OPEN_DELAY="0",
    )
    deadline = time.monotonic() + 30
    while not any(e.get("name") == "strateg fetch" for e in journal_of(race)):  # «начато» записано: идёт сеть
        assert time.monotonic() < deadline and slow.poll() is None
        time.sleep(0.1)
    start = time.monotonic()
    code, _, err = run(
        projects, "event", "error", "--message", "сосед", "--project", PID, STRATEG_LOCK_TIMEOUT="2"
    )
    assert code == 0, err  # папка свободна, хотя сеть первого ещё идёт
    assert time.monotonic() - start < 5 and slow.poll() is None
    assert finish([slow])[0][0] == 0
    assert any(e["kind"] == "ошибка" and e.get("message") == "сосед" for e in journal_of(race))
    assert_clean(race)


# --- кэш загрузчика пишется атомарно ---------------------------


def test_fetch_cache_is_replaced_atomically_and_leaves_no_temporary_files(tmp_path, monkeypatch):
    from strateg import fetch

    written = []
    real = storage.atomic_write_bytes
    monkeypatch.setattr(
        storage,
        "atomic_write_bytes",
        lambda path, data, timeout=None: (written.append(path), real(path, data, timeout)),
    )
    cache = tmp_path / ".cache" / "fetch.json"
    fetcher = fetch.Fetcher(cache, client=object())
    fetcher._save({"last_request": {"example.com": 1.5}})
    assert written == [cache]
    assert json.loads(cache.read_text(encoding="utf-8"))["last_request"]["example.com"] == 1.5
    assert [p.name for p in cache.parent.iterdir()] == ["fetch.json"]
