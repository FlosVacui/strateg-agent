"""Запись журнала и `state.yaml`: хуки и команды CLI пишут по очереди, под общей блокировкой папки проекта."""

from __future__ import annotations

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
from strateg.project_check import check_project

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
