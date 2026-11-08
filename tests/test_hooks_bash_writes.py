"""Запись через Bash: `cd`, git, dd, copy, ren, `sed -Ei`, `>|`, флаги, файлы состояния, блокировка, учёт."""

from __future__ import annotations

import pytest

from strateg.hooks import shell
from test_hooks_paths import LINUX, NT
from test_hooks_rules import allowed, decide, refused
from test_hooks_rules_zones import DEV, with_active

ROOT = LINUX.root


def bash(command: str, *, env=None, penv=LINUX, tool: str = "Bash"):
    return decide(tool, {"command": command}, env=env, penv=penv, svc=with_active("demo"))


# --- cd, pushd, Set-Location в команде с записью ---


@pytest.mark.parametrize(
    "command",
    [
        f"echo x > {ROOT}/src/a.py; cd docs",
        f"cd docs && echo x > {ROOT}/src/a.py",
        "echo x > src/a.py; cd docs",
        "cd src && echo x > a.py",
        "cd projects/demo/outputs && echo x > ../../../src/a.py",
        "cd projects/demo/outputs && echo x > a.md",  # даже безвредная: пути после cd не определить
        "pushd src; echo x > a.py; popd",
        "Set-Location src; Set-Content a.py x",
        "sl src; 'x' | Out-File a.py",
    ],
)
def test_write_together_with_cd_is_refused_with_a_hint(command):
    d = refused(bash(command), "cd")
    assert "без `cd`" in d.reason.lower() or "Пиши без" in d.reason


@pytest.mark.parametrize(
    "command",
    ["cd src", "cd projects/demo && ls", "cd projects/demo; git status", "Set-Location docs; Get-ChildItem"],
)
def test_cd_without_a_write_is_fine(command):
    allowed(bash(command))


def test_dev_mode_does_not_refuse_cd_with_a_write():
    allowed(bash("cd src && echo x > a.py", env=DEV))


def test_changes_directory_sees_wrappers_and_assignments():
    assert shell.changes_directory("FOO=1 cd x")
    assert shell.changes_directory("ls && pushd x")
    assert not shell.changes_directory("echo cd")
    assert not shell.changes_directory("git commit -m 'cd src'")


# --- git, dd, copy, xcopy, ren, sed -Ei, >| ---

CLOSED = [
    "git checkout -- src/a.py",
    "git checkout main",
    "git checkout -b feature",
    "git restore config/routing.yaml",
    "git restore --staged src",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git -C . reset --hard",
    "git rm src/a.py",
    "git mv src/a.py src/b.py",
    "dd if=/dev/zero of=src/a.py",
    "dd of=knowledge/x.md if=a",
    "copy a.txt src\\a.py",
    "copy /y a.txt src\\a.py",
    "xcopy a.txt src\\ /y",
    "xcopy /E /I a src\\b",
    "ren src\\a.py b.py",
    "rename knowledge\\x.md y.md",
    "Rename-Item src/a.py b.py",
    "Rename-Item -Path src/a.py -NewName b.py",
    "rni src/a.py b.py",
    "sed -Ei 's/a/b/' src/a.py",
    "sed -ni 's/a/b/p' src/a.py",
    "sed -i.bak -E 's/a/b/' src/a.py",
    "sed -ie 's/a/b/' src/a.py",
    "echo x >| src/a.py",
    "echo x >|src/a.py",
    "ls >| README.md",
]
OPEN = [
    "git status",
    "git diff",
    "git log --oneline -n 3",
    "git show HEAD:README.md",
    "git commit -m 'x'",
    "dd if=a of=/dev/null",
    "dd if=a of=projects/demo/outputs/a.bin",
    "copy a.txt projects/demo/outputs\\a.txt",
    "xcopy a projects\\demo\\outputs\\b /y",
    "ren projects\\demo\\outputs\\a.md b.md",
    "Rename-Item projects/demo/outputs/a.md b.md",
    "sed -n 's/a/b/p' src/a.py",
    "sed -E 's/a/b/' src/a.py",
    "sed -Ei 's/a/b/' projects/demo/outputs/a.md",
    "echo x >| projects/demo/outputs/a.md",
    "echo x >| /dev/null",
    "ls | grep x",
]


@pytest.mark.parametrize("command", CLOSED)
def test_write_command_outside_the_zone_is_refused(command):
    refused(bash(command), "outputs/")


@pytest.mark.parametrize("command", OPEN)
def test_reading_and_writing_inside_the_zone_stays_open(command):
    allowed(bash(command))


def test_pipe_after_a_redirect_symbol_is_not_a_separate_command():
    # `>|` — перенаправление; конвейер `a | b` остаётся конвейером
    assert shell.write_targets("echo x >| src/a.py") == ["src/a.py"]
    assert shell.write_targets("cat a | tee src/a.py") == ["src/a.py"]
    assert shell.write_targets("echo x | cat") == []


# --- ложные отказы в outputs/ ---


@pytest.mark.parametrize(
    "command",
    [
        "sed -i -e 's/a/b/' -e 's/c/d/' projects/demo/outputs/a.md",
        "sed -i --expression='s/a/b/' projects/demo/outputs/a.md",
        "sed -i -es/a/b/ projects/demo/outputs/a.md",
        "sed -i -f script.sed projects/demo/outputs/a.md",
        "touch -d '2020-01-01' projects/demo/outputs/a.md",
        "touch -t 202001010000 projects/demo/outputs/a.md",
        "touch -r other projects/demo/outputs/a.md",
        "mkdir -m 755 projects/demo/outputs/d",
        "mkdir -p -m 700 projects/demo/outputs/d",
        "truncate -s 0 projects/demo/outputs/a.md",
        "install -m 644 a projects/demo/outputs/a",
        "echo x > /dev/tty",
        "echo x > /dev/null",
    ],
)
def test_values_of_flags_are_not_paths(command):
    allowed(bash(command))


@pytest.mark.parametrize(
    "command",
    [
        "sed -i -e 's/a/b/' -e 's/c/d/' src/a.py",
        "touch -d '2020-01-01' src/a.py",
        "mkdir -m 755 src/d",
        "truncate -s 0 src/a.py",
    ],
)
def test_the_real_target_is_still_checked_after_a_flag_value(command):
    refused(bash(command), "outputs/")


def test_glob_with_an_absolute_pattern_with_a_drive_is_an_explicit_path():
    d = decide(
        "Glob",
        {"pattern": "D:/Home/Иван Петров/strateg agent/projects/demo/outputs/*.md"},
        penv=NT,
        svc=with_active("demo"),
    )
    allowed(d)
    d = decide(
        "Glob",
        {"pattern": "D:\\Home\\Иван Петров\\strateg agent\\projects\\demo\\outputs\\*.md"},
        penv=NT,
        svc=with_active("demo"),
    )
    allowed(d)
    refused(decide("Glob", {"pattern": "**/*.md"}, penv=NT, svc=with_active("demo")), "path")


# --- файлы состояния через Bash закрыты и в режиме разработки ---

STATE = [
    "echo x > projects/demo/state.yaml",
    "echo x >> projects/demo/journal.jsonl",
    "rm projects/demo/state.yaml",
    "rm projects/demo/claims.yaml",
    "sed -i 's/a/b/' projects/demo/options.yaml",
    "cp a projects/demo/sources.yaml",
    "mv projects/demo/frame.yaml /tmp/x",
    "rm -rf projects/demo/sources",
    "rm projects/demo/history/claims.v1.yaml",
    "Set-Content projects/demo/state.yaml x",
    "Remove-Item projects/demo/cost.jsonl",
]


@pytest.mark.parametrize("env", [None, DEV])
@pytest.mark.parametrize("command", STATE)
def test_bash_cannot_write_cli_state_files_even_in_dev_mode(command, env):
    refused(bash(command, env=env), "strateg")


@pytest.mark.parametrize("env", [None, DEV])
def test_bash_pending_stays_closed_in_dev_mode(env):
    refused(bash("echo x > projects/demo/.tmp/pending/a.txt", env=env), ".tmp/pending/")


def test_dev_mode_still_allows_writing_code_through_bash():
    allowed(bash("echo x > src/a.py", env=DEV))
    allowed(bash("sed -i 's/a/b/' README.md", env=DEV))


# --- .tmp/lock ---


@pytest.mark.parametrize("env", [None, DEV])
@pytest.mark.parametrize(
    "command",
    [
        "echo x > projects/demo/.tmp/lock",
        "rm projects/demo/.tmp/lock",
        "rm -rf projects/demo/.tmp",
        "mv projects/demo/.tmp /tmp/x",
        "Remove-Item -Recurse projects/demo/.tmp",
    ],
)
def test_lock_file_and_tmp_folder_cannot_be_written_or_removed_through_bash(command, env):
    refused(bash(command, env=env), "блокировки")


@pytest.mark.parametrize("env", [None, DEV])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_lock_file_cannot_be_written_by_file_tools(tool, env):
    tin = {"file_path": "projects/demo/.tmp/lock", "content": "x", "old_string": "a", "new_string": "b"}
    refused(decide(tool, tin, env=env, svc=with_active("demo")), "блокировки")


def test_other_files_in_tmp_stay_writable_and_removable():
    allowed(bash("echo x > projects/demo/.tmp/claim.yaml"))
    allowed(bash("rm projects/demo/.tmp/claim.yaml"))
    allowed(bash("rm -rf projects/demo/.tmp/dir"))
    allowed(
        decide(
            "Write", {"file_path": "projects/demo/.tmp/lock.yaml", "content": "x"}, svc=with_active("demo")
        )
    )


# --- .tmp/cost/ ---


@pytest.mark.parametrize("env", [None, DEV])
@pytest.mark.parametrize(
    "command",
    [
        "echo '{}' > projects/demo/.tmp/cost/offsets.json",
        "rm projects/demo/.tmp/cost/offsets.json",
        "rm -rf projects/demo/.tmp/cost",
        "mv projects/demo/.tmp/cost /tmp/x",
        "cp /tmp/x.json projects/demo/.tmp/cost/offsets.json",
        "Set-Content projects/demo/.tmp/cost/offsets.json x",
        "Remove-Item -Recurse projects/demo/.tmp/cost",
    ],
)
def test_cost_service_folder_cannot_be_written_or_removed_through_bash(command, env):
    """Иначе агент перепишет `exhausted` или `recounted_at` и обойдёт бюджет на неплатных вызовах."""
    refused(bash(command, env=env), ".tmp/cost/", "учёт стоимости")


@pytest.mark.parametrize("env", [None, DEV])
@pytest.mark.parametrize("agent", [None, "critic", "researcher"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_cost_service_file_cannot_be_written_by_file_tools(tool, agent, env):
    tin = {
        "file_path": "projects/demo/.tmp/cost/offsets.json",
        "content": "{}",
        "old_string": "a",
        "new_string": "b",
    }
    d = decide(tool, tin, agent, env=env, svc=with_active("demo"))
    # критику и исследователю запись закрыта целиком — отказ по роли раньше проверки файлов CLI
    refused(d, *((".tmp/cost/",) if agent is None else ()))


def test_cost_service_folder_closed_on_windows_in_any_case():
    refused(
        decide(
            "Write",
            {"file_path": r"projects\demo\.TMP\Cost\offsets.json", "content": "{}"},
            penv=NT,
            svc=with_active("demo"),
        ),
        ".tmp/cost/",
    )


def test_files_next_to_cost_service_folder_stay_writable():
    allowed(bash("echo x > projects/demo/.tmp/cost.yaml"))
    allowed(bash("echo x > projects/demo/.tmp/costs/a.yaml"))
    allowed(
        decide(
            "Write", {"file_path": "projects/demo/.tmp/cost-note.md", "content": "x"}, svc=with_active("demo")
        )
    )


def test_cost_service_folder_name_matches_the_offsets_module():
    from strateg.hooks import rules
    from strateg.offsets import SERVICE_DIR

    assert rules.COST_OWNED == SERVICE_DIR + "/"
