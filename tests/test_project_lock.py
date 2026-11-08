"""Запись журнала и `state.yaml`: хуки и команды CLI пишут по очереди, под общей блокировкой папки проекта."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from helpers import journal_of
from strateg import project as proj
from strateg import storage
from strateg.hooks import effects, rules
from strateg.models import CostRecord
from strateg.project_check import check_project
from transcripts import assistant

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "pre_tool_use.py"
DENY_EVENT = {
    "hook_event_name": "PreToolUse",
    "session_id": "s-race",
    "cwd": "",
    "tool_name": "WebFetch",
    "tool_input": {"url": "http://example.com"},
}


def child_env(projects: Path, **extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("STRATEG_", "CLAUDE_"))}
    return {
        **base,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "CLAUDE_PROJECT_DIR": str(ROOT),
        "STRATEG_PROJECTS_DIR": str(projects),
        **extra,
    }


def run_cli(projects: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "strateg.cli", *argv],
        capture_output=True,
        env=child_env(projects),
        cwd=ROOT,
        timeout=120,
    )


def run_hook(projects: Path, event: dict = DENY_EVENT, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOK)],
        input=json.dumps(event).encode("utf-8"),
        capture_output=True,
        env=child_env(projects, **env),
        cwd=ROOT,
        timeout=120,
    )


@pytest.fixture
def projects(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def race(projects) -> Path:
    assert run_cli(projects, "new", "race").returncode == 0
    return projects / "race"


def assert_consistent(root: Path) -> None:
    """Ни ложного «изменено вне агента», ни битого журнала."""
    kinds = [e["kind"] for e in journal_of(root)]
    assert "изменено вне агента" not in kinds
    assert check_project(root) == []
    assert proj.Project.load(root).outside == []


# --- параллельные процессы --------------------


def test_parallel_hooks_and_cli_lose_nothing_and_never_report_outside_changes(race, projects):
    hooks_per_worker, workers, cli_runs = 5, 3, 5

    def hook_worker(_: int) -> int:
        denied = 0
        for _i in range(hooks_per_worker):
            proc = run_hook(projects)
            assert proc.returncode == 0 and proc.stderr == b"", proc.stderr
            denied += json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        return denied

    def cli_worker() -> None:
        for _i in range(cli_runs):
            proc = run_cli(projects, "step", "start", "1")
            assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        cli_future = pool.submit(cli_worker)
        denied = sum(pool.map(hook_worker, range(workers)))
        cli_future.result()
    assert denied == workers * hooks_per_worker
    events = journal_of(race)
    assert sum(e["kind"] == "отказ хука" for e in events) == workers * hooks_per_worker
    assert_consistent(race)


def test_parallel_append_event_from_many_processes(race, projects):
    code = (
        "import sys; from pathlib import Path\n"
        "from strateg.hooks import effects, rules\n"
        "for i in range(int(sys.argv[2])):\n"
        "    ok = effects.append_event(Path(sys.argv[1]), rules.EventSpec('отказ хука', 'проба', name='x'))\n"
        "    assert ok\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", code, str(race), "15"],
            env=child_env(projects),
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")
    assert sum(e["kind"] == "отказ хука" for e in journal_of(race)) == 60
    assert_consistent(race)


def test_parallel_threads_append_event_and_save_state(race):
    def hook(_: int) -> None:
        for _i in range(20):
            assert effects.append_event(race, rules.EventSpec("отказ хука", "проба", name="t"))

    def cli(_: int) -> None:
        for _i in range(20):
            p = proj.Project.load(race)
            p.add_event(p.event(kind="ошибка", message="проба CLI"))
            p.save_state()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(hook, range(2)))
        list(pool.map(cli, range(2)))
    events = journal_of(race)
    assert sum(e["kind"] == "отказ хука" for e in events) == 40
    assert sum(e.get("message") == "проба CLI" for e in events) == 40
    assert_consistent(race)


# --- блокировка --------------------

HOLDER = (
    "import sys, time; from pathlib import Path\n"
    "from strateg import storage\n"
    "with storage.project_lock(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.readline()\n"
)


@pytest.fixture
def held(race, projects):
    """Другой процесс держит папку проекта, пока тест не закроет его ввод."""
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(race)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=child_env(projects),
    )
    assert p.stdout.readline().strip() == b"held"
    yield race
    p.stdin.close()
    p.wait(timeout=30)


def test_lock_waits_for_the_other_process_and_then_succeeds(race, projects):
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(race)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=child_env(projects),
    )
    assert p.stdout.readline().strip() == b"held"
    threading.Timer(0.5, p.stdin.close).start()
    start = time.monotonic()
    with storage.project_lock(race, timeout=20):
        waited = time.monotonic() - start
    p.wait(timeout=30)
    assert 0.3 < waited < 15


def test_lock_timeout_is_an_error_with_a_clear_text_not_an_endless_wait(held):
    start = time.monotonic()
    with pytest.raises(storage.LockTimeout) as err, storage.project_lock(held, timeout=0.4):
        pass
    assert time.monotonic() - start < 5
    assert "занята" in str(err.value) and held.name in str(err.value)


def test_cli_reports_busy_project_as_cli_error(held, monkeypatch):
    monkeypatch.setenv("STRATEG_LOCK_TIMEOUT", "0.3")
    with pytest.raises(proj.CliError, match="занята"), proj.locked(held):
        pass


def test_cli_command_on_busy_project_fails_with_text(held, projects):
    proc = run_cli_with_timeout(projects, "status", "--brief")
    assert proc.returncode == 1
    assert "занята" in (proc.stdout + proc.stderr).decode("utf-8", "replace")


def run_cli_with_timeout(projects: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "strateg.cli", *argv],
        capture_output=True,
        env=child_env(projects, STRATEG_LOCK_TIMEOUT="0.5"),
        cwd=ROOT,
        timeout=120,
    )


def test_hook_denial_survives_a_busy_project_and_says_the_event_was_lost(held, projects):
    proc = run_hook(projects, STRATEG_LOCK_TIMEOUT="0.5")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "занята" in proc.stderr.decode("utf-8", "replace")  # потерянное событие не молчит


def test_hook_launching_a_role_is_refused_when_the_journal_is_busy(held, projects):
    event = {
        **DENY_EVENT,
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "critic", "prompt": "Режим: возражения."},
    }
    proc = run_hook(projects, event, STRATEG_LOCK_TIMEOUT="0.5")
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "занята" in out["permissionDecisionReason"]


def test_lock_is_nested_inside_one_process(race):
    with storage.project_lock(race, timeout=1), storage.project_lock(race, timeout=1):
        pass
    with storage.project_lock(race, timeout=1):  # и после выхода занять снова можно
        pass


# --- временные файлы и откат --------------------


def test_temp_files_are_unique_per_call_and_leave_nothing_behind(race):
    a, b = storage.temp_name(race / "state.yaml"), storage.temp_name(race / "state.yaml")
    assert a != b and a.name.endswith(".tmp") and a.name != "state.yaml.tmp"
    proj.write_text(race / "x.yaml", "a: 1\n")
    assert not [p.name for p in race.iterdir() if p.is_file() and p.name.endswith(".tmp")]


def test_concurrent_writers_of_one_file_do_not_clash_on_a_shared_temp_name(race):
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(40):
                proj.write_text(race / "shared.yaml", f"n: {n}\ni: {i}\n")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert not [p.name for p in race.iterdir() if p.is_file() and p.name.endswith(".tmp")]


def test_rollback_keeps_events_the_hook_wrote_during_the_command(race):
    before = journal_of(race)
    with pytest.raises(RuntimeError), proj.transaction(race):
        p = proj.Project.load(race)
        p.add_event(p.event(kind="ошибка", message="событие откатываемой команды"))
        assert effects.append_event(race, rules.EventSpec("отказ хука", "во время команды", name="WebFetch"))
        raise RuntimeError("команда упала")
    events = journal_of(race)
    assert [e["kind"] for e in events[len(before) :]] == ["отказ хука"]
    assert all(e.get("message") != "событие откатываемой команды" for e in events)
    assert_consistent(race)


# --- время события не убывает (под блокировкой) --------------------


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def test_hook_event_time_is_not_earlier_than_the_last_journal_line(race, monkeypatch):
    last = parse_time(journal_of(race)[-1]["time"])
    monkeypatch.setattr(effects, "now", lambda: last - dt.timedelta(seconds=5))
    assert effects.append_event(race, rules.EventSpec("отказ хука", "часы отстали", name="t"))
    written = journal_of(race)[-1]
    assert parse_time(written["time"]) == last
    assert_consistent(race)


def test_cli_event_created_before_a_later_hook_event_is_written_with_a_time_not_earlier(race, monkeypatch):
    p = proj.Project.load(race)
    ev = p.event(kind="ошибка", message="создано раньше")
    later = ev.time + dt.timedelta(seconds=10)
    monkeypatch.setattr(effects, "now", lambda: later)
    assert effects.append_event(race, rules.EventSpec("отказ хука", "позже", name="t"))
    p.add_event(ev)  # без правки времени это ошибка проверки «время раньше предыдущей строки»
    events = journal_of(race)
    assert [e["kind"] for e in events[-2:]] == ["отказ хука", "ошибка"]
    assert parse_time(events[-1]["time"]) == later
    assert p.journal[-1].time == later  # и в памяти команды то же время, что в журнале
    assert_consistent(race)


def test_transaction_keeps_event_order_when_a_hook_writes_while_the_command_waits(race, monkeypatch):
    with proj.transaction(race):
        p = proj.Project.load(race)
        ev = p.event(kind="ошибка", message="создано раньше")
        later = ev.time + dt.timedelta(seconds=30)
        monkeypatch.setattr(effects, "now", lambda: later)
        assert effects.append_event(race, rules.EventSpec("отказ хука", "позже", name="t"))
        p.add_event(ev)
        p.save_state()
    assert_consistent(race)


def test_clamp_time_helper():
    t0 = dt.datetime(2026, 11, 8, 10, 0, 0, tzinfo=dt.UTC)
    journal = json.dumps({"time": t0.isoformat(), "kind": "ошибка"}).encode() + b"\n"
    assert storage.clamp_time(t0 - dt.timedelta(seconds=1), journal) == t0
    assert storage.clamp_time(t0 + dt.timedelta(seconds=1), journal) == t0 + dt.timedelta(seconds=1)
    assert storage.clamp_time(t0, b"") == t0
    assert storage.clamp_time(t0, b"not json\n") == t0


def test_competing_processes_never_write_time_backwards(race, projects):
    code = (
        "import sys; from pathlib import Path\n"
        "from strateg.hooks import effects, rules\n"
        "for i in range(int(sys.argv[2])):\n"
        "    effects.append_event(Path(sys.argv[1]), rules.EventSpec('отказ хука', 'проба', name='x'))\n"
    )

    def hooks(_: int) -> None:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code, str(race), "6"],
            env=child_env(projects),
            capture_output=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    def cli_errors(_: int) -> None:
        for _i in range(4):
            proc = run_cli(projects, "event", "error", "--message", "проба", "--tool", "Bash")
            assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(hooks, i) for i in range(4)] + [pool.submit(cli_errors, i) for i in range(4)]
        for f in futures:
            f.result()
    times = [parse_time(e["time"]) for e in journal_of(race)]
    assert times == sorted(times)
    assert_consistent(race)


# --- state.yaml: повторы до таймаута, иначе понятная ошибка; журнал и хеш не расходятся --------------------


def test_replace_file_retries_until_the_timeout_then_says_what_is_wrong(tmp_path, monkeypatch):
    target = tmp_path / "state.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    tmp = tmp_path / "state.yaml.1.tmp"
    tmp.write_text("a: 2\n", encoding="utf-8")

    def busy(src, dst):
        raise PermissionError("[WinError 5] Отказано в доступе")

    monkeypatch.setattr(os, "replace", busy)
    start = time.monotonic()
    with pytest.raises(storage.FileBusy, match="занят") as err:
        storage.replace_file(tmp, target, timeout=0.4)
    assert 0.35 <= time.monotonic() - start < 5
    assert "state.yaml" in str(err.value) and not tmp.exists()


def test_replace_file_waits_longer_than_a_second_for_a_busy_reader(tmp_path, monkeypatch):
    target = tmp_path / "state.yaml"
    tmp = tmp_path / "state.yaml.1.tmp"
    tmp.write_text("a: 2\n", encoding="utf-8")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(time.monotonic())
        if time.monotonic() - calls[0] < 1.3:
            raise PermissionError("занят")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    storage.replace_file(tmp, target, timeout=5)  # раньше сдавались через секунду (40 повторов)
    assert target.read_text(encoding="utf-8") == "a: 2\n"


def test_journal_line_is_taken_back_when_state_yaml_cannot_be_replaced(race, monkeypatch):
    journal = race / "journal.jsonl"
    before = journal.read_bytes()

    def busy(path, data, timeout=None):
        raise storage.FileBusy(path, 1)

    monkeypatch.setattr(storage, "atomic_write_bytes", busy)
    with pytest.raises(storage.FileBusy):
        effects.append_event(race, rules.EventSpec("отказ хука", "не запишется", name="t"))
    assert journal.read_bytes() == before  # событие и хеш не расходятся
    monkeypatch.undo()
    assert_consistent(race)


def test_cli_write_reports_a_busy_file_as_cli_error(tmp_path, monkeypatch):
    def busy(src, dst):
        raise PermissionError("занят")

    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "0.2")
    monkeypatch.setattr(os, "replace", busy)
    with pytest.raises(proj.CliError, match="занят"):
        proj.write_text(tmp_path / "x.yaml", "a: 1\n")


@pytest.mark.skipif(sys.platform != "win32", reason="на Windows открытый читателем файл нельзя заменить")
def test_windows_reader_holding_state_yaml_does_not_leave_the_hash_behind(race, projects, monkeypatch):
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\nf = open(sys.argv[1], 'rb')\nprint('open', flush=True)\ntime.sleep(2.5)\n",
            str(race / "state.yaml"),
        ],
        stdout=subprocess.PIPE,
    )
    assert reader.stdout.readline().strip() == b"open"
    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "0.6")
    before = (race / "journal.jsonl").read_bytes()
    with pytest.raises(storage.FileBusy):
        effects.append_event(race, rules.EventSpec("отказ хука", "читатель держит файл", name="t"))
    assert (race / "journal.jsonl").read_bytes() == before
    reader.wait(timeout=30)
    monkeypatch.undo()
    assert_consistent(race)


# --- откат ждёт блокировку дольше; не дождался — называет файлы --------------------


def start_holder(race: Path, projects: Path) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(race)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=child_env(projects),
    )
    assert p.stdout.readline().strip() == b"held"
    return p


def test_rollback_waits_longer_than_an_ordinary_command():
    assert proj.ROLLBACK_LOCK_TIMEOUT > storage.LOCK_TIMEOUT


def test_rollback_that_cannot_take_the_lock_names_the_files_left_written(race, projects, monkeypatch):
    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "0.5")
    claims = race / "claims.yaml"
    holder = None
    try:
        with pytest.raises(proj.CliError) as err, proj.transaction(race):
            claims.write_text("garbage: [\n", encoding="utf-8")
            holder = start_holder(race, projects)
            raise RuntimeError("команда упала")
        text = str(err.value)
        assert "откат не выполнен" in text and "claims.yaml" in text and "команда упала" in text
        assert claims.read_text(encoding="utf-8") == "garbage: [\n"
    finally:
        if holder is not None:
            holder.stdin.close()
            holder.wait(timeout=30)


def test_rollback_waits_for_a_lock_released_soon(race, projects, monkeypatch):
    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "10")
    claims = race / "claims.yaml"
    original = claims.read_bytes()
    holder = None
    with pytest.raises(RuntimeError), proj.transaction(race):
        claims.write_text("garbage: [\n", encoding="utf-8")
        holder = start_holder(race, projects)
        threading.Timer(0.7, holder.stdin.close).start()
        raise RuntimeError("команда упала")
    holder.wait(timeout=30)
    assert claims.read_bytes() == original  # дождался и откатил


def test_transaction_snapshot_and_add_cost_take_the_lock(race, projects, monkeypatch):
    p = proj.Project.load(race)
    holder = start_holder(race, projects)
    try:
        monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "0.3")
        with pytest.raises(proj.CliError, match="занята"), proj.transaction(race):
            pass
        record = CostRecord(
            schema_version=1,
            time=proj.now(),
            session_id="s",
            role="main",
            model="sonnet",
            environment="claude-code",
            entry="итог сессии",
            cost_source="нет данных",
        )
        with pytest.raises(proj.CliError, match="занята"):
            p.add_cost(record)
        with pytest.raises(proj.CliError, match="занята"):
            p.add_event(p.event(kind="ошибка", message="x"))
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


# --- таймауты --------------------


def test_timeout_values_match_the_documentation(monkeypatch):
    monkeypatch.delenv(storage.LOCK_TIMEOUT_ENV, raising=False)
    assert storage.LOCK_TIMEOUT == 10.0 and storage.HOOK_LOCK_TIMEOUT == 5.0
    assert storage.lock_timeout() == 10.0
    assert storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT) == 5.0
    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "0.7")
    assert storage.lock_timeout() == 0.7 and storage.lock_timeout(storage.HOOK_LOCK_TIMEOUT) == 0.7
    monkeypatch.setenv(storage.LOCK_TIMEOUT_ENV, "не число")
    assert storage.lock_timeout() == 10.0


def test_two_hook_waits_fit_into_the_hook_timeout_of_the_environment():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    timeout = settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
    assert 2 * storage.HOOK_LOCK_TIMEOUT < timeout  # чтение проекта и запись события, затем ответ


def test_hook_passes_its_own_shorter_timeout_to_the_journal(race, monkeypatch):
    seen = []
    monkeypatch.delenv(storage.LOCK_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(storage, "append_journal", lambda root, build, timeout=None: seen.append(timeout))
    effects.append_event(race, rules.EventSpec("отказ хука", "x", name="t"))
    assert seen == [storage.HOOK_LOCK_TIMEOUT]


def test_hook_reads_the_project_for_a_role_launch_with_the_hook_timeout(race, projects, monkeypatch):
    seen = {}

    def fake_load(root, timeout=None):
        seen["timeout"] = timeout
        raise RuntimeError("стоп")

    monkeypatch.delenv(storage.LOCK_TIMEOUT_ENV, raising=False)
    monkeypatch.setenv("STRATEG_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(proj.Project, "load", staticmethod(fake_load))
    ctx = effects.make_ctx({"cwd": str(ROOT)}, {**os.environ, "STRATEG_PROJECTS_DIR": str(projects)})
    with pytest.raises(RuntimeError, match="стоп"):
        effects.LiveServices(ctx).project()
    assert seen["timeout"] == storage.HOOK_LOCK_TIMEOUT


def test_hook_with_a_busy_folder_answers_faster_than_the_environment_timeout(held, projects):
    event = {
        **DENY_EVENT,
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "critic", "prompt": "Режим: возражения."},
    }
    start = time.monotonic()
    proc = run_hook(projects, event)  # таймауты по умолчанию, как в настройках
    elapsed = time.monotonic() - start
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "5 с" in out["permissionDecisionReason"]
    assert elapsed < 9  # а не 11 с: хук Claude Code не дожидается больше 15 с и пропустил бы вызов


def test_budget_part_of_pre_tool_use_gives_up_within_its_deadline_when_the_folder_is_busy(
    held, projects, tmp_path
):
    """Занятая папка не должна держать Bash дольше срока бюджетной части: хук блокирует вызов по таймауту."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(assistant("m1", out=10) + chr(10), encoding="utf-8")
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-busy",
        "cwd": "",
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    start = time.monotonic()
    proc = run_hook(projects, event)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0 and proc.stdout == b"", proc.stdout  # вызов пропущен, а не отказан
    assert elapsed < 8  # срок бюджетной части 2 с и запись события до 0,5 с; было 4 ожидания по 5 с


# --- блокировку отпускает система --------------------

CRASHER = (
    "import os, sys; from pathlib import Path\n"
    "from strateg import storage\n"
    "with storage.project_lock(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    os._exit(1)\n"
)


def test_lock_is_released_when_the_holder_process_dies(race, projects):
    proc = subprocess.Popen(
        [sys.executable, "-c", CRASHER, str(race)], stdout=subprocess.PIPE, env=child_env(projects)
    )
    assert proc.stdout.readline().strip() == b"held"
    proc.wait(timeout=30)
    start = time.monotonic()
    with storage.project_lock(race, timeout=3):
        pass
    assert time.monotonic() - start < 2
    with proj.locked(race, 3):  # и команды CLI берут её сразу
        pass


def test_lock_is_released_when_the_holder_process_is_killed(race, projects):
    holder = start_holder(race, projects)
    holder.kill()
    holder.wait(timeout=30)
    with storage.project_lock(race, timeout=3):
        pass


# --- «граница прав не активна»: раз за сессию, проверка под блокировкой --------------------


def test_once_per_session_event_is_written_once_under_the_lock(race):
    spec = rules.EventSpec(effects.ONCE_PER_SESSION, "нет ролей", name="сессия-1")
    assert effects.append_event(race, spec) is True
    assert effects.append_event(race, spec) is False  # проверка внутри блокировки
    assert effects.append_event(race, rules.EventSpec(effects.ONCE_PER_SESSION, "нет ролей", name="сессия-2"))
    kinds = [(e["kind"], e.get("name")) for e in journal_of(race)]
    assert kinds.count((effects.ONCE_PER_SESSION, "сессия-1")) == 1
    assert kinds.count((effects.ONCE_PER_SESSION, "сессия-2")) == 1
    assert_consistent(race)


def test_parallel_foreign_hooks_of_one_session_write_one_event(race, projects):
    event = {
        "hook_event_name": "preToolUse",  # не Claude Code
        "session_id": "s-cursor",
        "cursor_version": "1.0",
        "cwd": "",
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
    }

    def one(_: int) -> None:
        proc = run_hook(projects, event)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(8)))
    events = [e for e in journal_of(race) if e["kind"] == "граница прав не активна"]
    assert len(events) == 1 and events[0]["name"] == "s-cursor"
    assert_consistent(race)
