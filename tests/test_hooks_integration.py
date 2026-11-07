"""Скрипты хуков запускаются подпроцессом (JSON на stdin, папка с кириллицей и пробелами); settings.json."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from strateg.hooks import rules

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
HOOKS = ROOT / ".claude" / "hooks"
FOLDER = "Папка проекта с пробелами и кириллицей"


@pytest.fixture
def product(tmp_path) -> Path:
    """Копия продукта в папке с кириллицей и пробелом: исходники, настройки, обёртки, роли, проект."""
    dest = tmp_path / FOLDER
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src", dest / "src", ignore=ignore)
    shutil.copytree(ROOT / "config", dest / "config")
    shutil.copytree(HOOKS, dest / ".claude" / "hooks", ignore=ignore)
    shutil.copytree(ROOT / ".claude" / "agents", dest / ".claude" / "agents")
    shutil.copy(ROOT / "pyproject.toml", dest / "pyproject.toml")
    (dest / "projects").mkdir()
    return dest


def run_hook(product: Path, script: str, event: dict, **env) -> subprocess.CompletedProcess:
    full = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("STRATEG_", "CLAUDE_"))},
        "PYTHONPATH": str(product / "src"),
        "PYTHONUTF8": "1",
        "CLAUDE_PROJECT_DIR": str(product),
        "STRATEG_PROJECTS_DIR": str(product / "projects"),
        **env,
    }
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(product / ".claude" / "hooks" / script)],
        input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=full,
        cwd=product,
        timeout=120,
    )


def pre_event(tool, tin=None, **extra):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s-1",
        "cwd": "",
        "tool_name": tool,
        "tool_input": tin or {},
        **extra,
    }


def body(proc) -> dict:
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stderr == b""
    text = proc.stdout.decode("utf-8")  # только UTF-8
    return json.loads(text)["hookSpecificOutput"]  # и только один объект JSON


def test_hook_script_in_cyrillic_folder_with_spaces(product):
    cwd = str(product)
    code = "import strateg, sys; print(strateg.__file__)"  # проверка: взята копия, а не рабочая папка
    check = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(product / "src"), "PYTHONUTF8": "1"},
        capture_output=True,
        cwd=cwd,
    )
    assert FOLDER in check.stdout.decode("utf-8", "replace")

    proc = run_hook(product, "pre_tool_use.py", pre_event("WebFetch", {"url": "https://пример.рф"}, cwd=cwd))
    out = body(proc)
    assert out["permissionDecision"] == "deny" and "WebFetch" in out["permissionDecisionReason"]
    assert out["hookEventName"] == "PreToolUse"


def test_main_agent_launches_general_purpose_in_cyrillic_folder(product):
    proc = run_hook(
        product,
        "pre_tool_use.py",
        pre_event("Agent", {"subagent_type": "general-purpose", "prompt": "найди"}, cwd=str(product)),
    )
    out = body(proc)
    assert out["permissionDecision"] == "deny" and "researcher" in out["permissionDecisionReason"]


def test_researcher_curl_and_inputs_in_cyrillic_folder(product):
    cwd = str(product)
    inputs = str(product / "projects" / "demo" / "inputs" / "заметка.txt")
    curl = run_hook(
        product,
        "pre_tool_use.py",
        pre_event("Bash", {"command": "curl https://x.com"}, cwd=cwd, agent_type="researcher", agent_id="a"),
    )
    assert body(curl)["permissionDecision"] == "deny"
    read = run_hook(
        product,
        "pre_tool_use.py",
        pre_event("Read", {"file_path": inputs}, cwd=cwd, agent_type="researcher", agent_id="a"),
    )
    assert body(read)["permissionDecision"] == "deny"
    sources = str(product / "projects" / "demo" / "sources" / "страница.txt")
    ok = run_hook(
        product,
        "pre_tool_use.py",
        pre_event("Read", {"file_path": sources}, cwd=cwd, agent_type="researcher", agent_id="a"),
    )
    assert ok.returncode == 0 and ok.stdout == b"" and ok.stderr == b""


def test_allowed_call_prints_nothing(product):
    proc = run_hook(product, "pre_tool_use.py", pre_event("Read", {"file_path": str(product / "README.md")}))
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def test_hooks_off_prints_nothing_even_when_package_is_missing(product):
    event = pre_event("WebFetch", {"url": "https://x.com"})
    proc = run_hook(product, "pre_tool_use.py", event, STRATEG_HOOKS="0")
    assert proc.returncode == 0 and proc.stdout == b""
    shutil.rmtree(product / "src")  # пакета нет, но хуки выключены
    proc = run_hook(product, "pre_tool_use.py", event, STRATEG_HOOKS="0")
    assert proc.returncode == 0 and proc.stdout == b""


def test_wrapper_denies_when_package_cannot_be_imported(product, tmp_path):
    """Сбой до решения (нет пакета): обёртка сама отвечает отказом, а не пропускает вызов."""
    lonely = tmp_path / "лишь обёртка"
    lonely.mkdir()
    shutil.copy(HOOKS / "pre_tool_use.py", lonely / "pre_tool_use.py")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(lonely / "pre_tool_use.py")],
        input=json.dumps(pre_event("Read", {"file_path": "README.md"})).encode("utf-8"),
        capture_output=True,
    )
    out = body(proc)
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith(
        "хук прав не сработал"
    )


def test_pre_tool_use_wrapper_with_garbage_input_denies(product):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(product / ".claude" / "hooks" / "pre_tool_use.py")],
        input=b"not json at all",
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(product / "src"), "CLAUDE_PROJECT_DIR": str(product)},
    )
    assert body(proc)["permissionDecision"] == "deny"


def test_session_start_script_gives_context(product):
    cli = [sys.executable, "-X", "utf8", "-m", "strateg.cli"]
    env = {
        **os.environ,
        "PYTHONPATH": str(product / "src"),
        "PYTHONUTF8": "1",
        "STRATEG_PROJECTS_DIR": str(product / "projects"),
    }
    subprocess.run([*cli, "new", "demo"], env=env, cwd=product, capture_output=True, check=True)
    event = {"hook_event_name": "SessionStart", "session_id": "s", "source": "startup", "cwd": str(product)}
    out = body(run_hook(product, "session_start.py", event))
    assert out["hookEventName"] == "SessionStart"
    assert "Проект: demo" in out["additionalContext"]


def test_session_start_script_without_project(product):
    event = {"hook_event_name": "SessionStart", "session_id": "s", "source": "compact", "cwd": str(product)}
    out = body(run_hook(product, "session_start.py", event))
    assert out["additionalContext"] == "Проекта нет, начни с `strateg new`."


def test_failure_script_writes_to_journal(product):
    cli = [sys.executable, "-X", "utf8", "-m", "strateg.cli"]
    env = {
        **os.environ,
        "PYTHONPATH": str(product / "src"),
        "PYTHONUTF8": "1",
        "STRATEG_PROJECTS_DIR": str(product / "projects"),
    }
    subprocess.run([*cli, "new", "demo"], env=env, cwd=product, capture_output=True, check=True)
    event = {
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": "x"},
        "error": "Exit code 1\nПапка не найдена",
        "cwd": str(product),
    }
    proc = run_hook(product, "post_tool_use_failure.py", event)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    journal = (product / "projects" / "demo" / "journal.jsonl").read_text(encoding="utf-8")
    last = json.loads(journal.strip().splitlines()[-1])
    assert last["kind"] == "ошибка" and last["actor"] == "хук" and "Папка не найдена" in last["message"]


def test_failure_script_never_blocks_on_garbage(product):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(product / ".claude" / "hooks" / "post_tool_use_failure.py")],
        input=b"\xff\xfe garbage",
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(product / "src")},
    )
    assert proc.returncode == 0 and proc.stdout == b""


# --- .claude/settings.json ----------------------------------------------------------------------------------


def hook_entries(event: str) -> list[dict]:
    return [h for group in SETTINGS["hooks"][event] for h in group["hooks"]]


def test_settings_cover_the_events_of_the_adr_table():
    assert set(SETTINGS["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUseFailure"}
    assert SETTINGS["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"


def test_settings_commands_are_exec_form_with_utf8_and_existing_scripts():
    for event, script in (
        ("SessionStart", "session_start.py"),
        ("PreToolUse", "pre_tool_use.py"),
        ("PostToolUseFailure", "post_tool_use_failure.py"),
    ):
        (hook,) = hook_entries(event)
        assert (
            hook["type"] == "command" and hook["command"] == "uv"
        )  # без оболочки: пути с пробелами и кириллицей
        args = hook["args"]
        assert args[:2] == ["run", "--frozen"] and "-X" in args and args[args.index("-X") + 1] == "utf8"
        assert "${CLAUDE_PROJECT_DIR}" in args[args.index("--project") + 1]
        assert args[-1] == "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + script
        assert (HOOKS / script).is_file()
    assert SETTINGS["env"]["PYTHONUTF8"] == "1"


def test_permission_hook_blocks_on_failure_others_do_not():
    assert hook_entries("PreToolUse")[0]["onFailure"] == "block"
    assert hook_entries("SessionStart")[0]["onFailure"] == "continue"
    assert hook_entries("PostToolUseFailure")[0]["onFailure"] == "continue"


@pytest.mark.parametrize(
    "tool",
    [
        "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "Monitor",
        "Agent", "Task", "WebSearch", "WebFetch",
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_take_screenshot",
        "mcp__plugin_playwright_playwright__browser_click",
    ],
)  # fmt: skip
def test_pre_tool_use_matcher_covers_tools(tool):
    assert re.search(SETTINGS["hooks"]["PreToolUse"][0]["matcher"], tool), tool


@pytest.mark.parametrize(
    "tool", ["TodoWrite", "TaskCreate", "ReadMcpResource", "Skill", "mcp__memory__search"]
)
def test_pre_tool_use_matcher_does_not_run_on_unrelated_tools(tool):
    assert not re.search(SETTINGS["hooks"]["PreToolUse"][0]["matcher"], tool), tool


def test_matcher_covers_every_tool_the_rules_know():
    matcher = SETTINGS["hooks"]["PreToolUse"][0]["matcher"]
    known = (
        rules.READ_TOOLS
        | rules.WRITE_TOOLS
        | rules.SHELL_TOOLS
        | rules.AGENT_TOOLS
        | {"WebSearch", "WebFetch"}
    )
    for tool in known:
        assert re.search(matcher, tool), tool


def test_deny_rules_close_webfetch_and_builtin_subagents_and_secrets():
    deny = SETTINGS["permissions"]["deny"]
    assert "WebFetch" in deny
    for name in rules.BUILTIN_AGENTS:
        assert f"Agent({name})" in deny
    for rule in ("Read(.env)", "Read(~/.strateg/**)", "Read(**/.tmp/pending/**)", "Read(evals/**)"):
        assert rule in deny
    assert deny.index("Read(!.env.example)") > deny.index("Read(.env.*)")  # исключение — после правила
    # охрана правил отключается STRATEG_DEV, а запрет в настройках отключить нельзя: его здесь нет
    assert not any("AGENTS.md" in r or "CLAUDE.md" in r or ".claude" in r for r in deny)


def test_status_line_uses_the_cli():
    line = SETTINGS["statusLine"]
    assert line["type"] == "command" and line["command"].endswith("strateg status --line")


def test_wrappers_are_thin():
    for path in HOOKS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "strateg.hooks" in text and len(text.splitlines()) <= 60, path.name
        assert "print(" not in text  # ответ только через stdout.buffer: ни одной лишней строки


@pytest.mark.skipif(shutil.which("uv") is None, reason="нужен uv")
def test_settings_command_really_runs_and_denies_webfetch():
    """Команда из settings.json с подставленным `${CLAUDE_PROJECT_DIR}` рабочая, а не только похожа на неё."""
    (hook,) = hook_entries("PreToolUse")
    argv = [hook["command"], *(a.replace("${CLAUDE_PROJECT_DIR}", str(ROOT)) for a in hook["args"])]
    proc = subprocess.run(
        argv,
        input=json.dumps(pre_event("WebFetch", {"url": "https://x.com"})).encode(),
        capture_output=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        cwd=ROOT.parent,  # хук запущен не из корня: `--project` находит проект сам
        timeout=300,
    )
    assert body(proc)["permissionDecision"] == "deny"
