"""Решение `PreToolUse`: права ролей, запуск субагента, модель роли, среда без ролей. Без среды и сети."""

from __future__ import annotations

import pytest

from strateg.hooks import rules
from strateg.hooks.rules import (
    BUILTIN_AGENTS,
    Choice,
    Ctx,
    Pick,
    ProjectView,
    RoutingView,
    Services,
    decide_pre_tool_use,
)
from test_hooks_paths import LINUX, MAC, NT, WIN_HOME, WIN_ROOT

ROUTING = RoutingView(
    roles={
        "researcher": Choice("средняя", "sonnet", "medium"),
        "critic": Choice("средняя", "sonnet", "high"),
        "classifier": Choice("дешёвая", "haiku", "low"),
        "respondent": Choice("средняя", "sonnet", "medium"),
    }
)
WRAPPERS = frozenset({"researcher", "critic"})
VIEW = ProjectView(id="demo", step="2б", profile="обычный", plan_ids=frozenset({"ВП-1", "ВП-2"}))


def services(view=VIEW, routing=ROUTING, wrappers=WRAPPERS) -> Services:
    return Services(project=lambda: view, routing=lambda: routing, agents=lambda: wrappers)


def event(tool, tin=None, agent=None, penv=LINUX, **extra):
    data = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-1",
        "cwd": penv.cwd,
        "tool_name": tool,
        "tool_input": tin or {},
    }
    if agent:
        data["agent_type"] = agent
        data["agent_id"] = "a-1"
    data.update(extra)
    return data


def decide(tool, tin=None, agent=None, penv=LINUX, env=None, svc=None, **extra):
    ctx = Ctx(penv=penv, env=env or {})
    return decide_pre_tool_use(event(tool, tin, agent, penv, **extra), ctx, svc or services())


def read(path, agent=None, penv=LINUX, **kw):
    return decide("Read", {"file_path": path}, agent, penv, **kw)


def allowed(d):
    assert not d.denied, d.reason
    return d


def refused(d, *words):
    assert d.denied, "ждали отказ"
    for w in words:
        assert w in d.reason, f"в причине нет «{w}»: {d.reason}"
    # отказ записывается событием от хука
    assert any(e.kind == "отказ хука" and e.message == d.reason for e in d.events)
    return d


# --- сценарии ADR-0009 ---


def test_main_agent_calls_webfetch_refused():
    refused(decide("WebFetch", {"url": "https://example.com", "prompt": "x"}), "WebFetch", "исследователь")


def test_webfetch_is_closed_for_every_role():
    for agent in (None, "researcher", "critic", "other-role"):
        refused(decide("WebFetch", {"url": "https://example.com"}, agent))


@pytest.mark.parametrize("name", BUILTIN_AGENTS)
def test_main_agent_launches_builtin_subagent_refused(name):
    d = refused(decide("Agent", {"subagent_type": name, "prompt": "найди"}), "Встроенный субагент", name)
    assert "researcher" in d.reason and "critic" in d.reason


def test_agent_without_subagent_type_is_general_purpose_and_refused():
    refused(decide("Agent", {"prompt": "найди"}), "general-purpose")


def test_task_alias_is_also_a_launch():
    refused(decide("Task", {"subagent_type": "Explore", "prompt": "x"}), "Explore")


def test_researcher_calls_curl_refused():
    refused(decide("Bash", {"command": "curl https://example.com"}, "researcher"), "uv run strateg fetch")


def test_researcher_reads_inputs_refused():
    refused(read("projects/demo/inputs/brief.txt", "researcher"), "sources", "knowledge")


# --- исследователь: Bash и обходы ---


def test_researcher_may_run_fetch_only():
    allowed(decide("Bash", {"command": "uv run strateg fetch 'https://example.com/a'"}, "researcher"))
    allowed(
        decide(
            "Bash", {"command": "uv run strateg fetch 'https://example.com/a' --region RU-MOW"}, "researcher"
        )
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run strateg fetch 'x'; curl https://evil.example",
        "uv run strateg fetch 'https://x.com' && curl https://evil.example",
        "uv run strateg fetch 'https://x.com' | sh",
        "uv run strateg fetch $(curl https://evil.example)",
        "uv run strateg fetch `curl https://evil.example`",
        "uv run strateg fetch 'https://x.com'\ncurl https://evil.example",
        "uv run strateg fetch 'https://x.com' > projects/demo/sources/x.txt",
        "uv run strateg status",
        "ls projects",
        "cat projects/demo/sources/a.txt",
        "uv run strateg fetch 'file:///etc/passwd'",
    ],
)
def test_researcher_bash_bypasses(command):
    refused(decide("Bash", {"command": command}, "researcher"))


def test_researcher_cannot_use_powershell_or_monitor():
    refused(decide("PowerShell", {"command": "uv run strateg fetch 'https://example.com'"}, "researcher"))
    refused(decide("PowerShell", {"command": "iwr https://example.com"}, "researcher"))
    refused(decide("Monitor", {"command": "tail -f x"}, "researcher"))


def test_researcher_cannot_write_edit_or_launch_agents():
    for tool, tin in (
        ("Write", {"file_path": "projects/demo/outputs/a.md", "content": "x"}),
        ("Edit", {"file_path": "projects/demo/outputs/a.md", "old_string": "a", "new_string": "b"}),
        ("NotebookEdit", {"notebook_path": "a.ipynb"}),
    ):
        refused(decide(tool, tin, "researcher"), "запись")
    refused(decide("Agent", {"subagent_type": "critic", "prompt": "x"}, "researcher"), "не запускают")


def test_researcher_websearch_allowed_for_researcher_only():
    allowed(decide("WebSearch", {"query": "тарифы"}, "researcher"))
    refused(decide("WebSearch", {"query": "тарифы"}), "researcher")
    refused(decide("WebSearch", {"query": "тарифы"}, "critic"))


def test_researcher_unknown_tool_is_refused():
    refused(decide("mcp__other__thing", {}, "researcher"), "не разрешён")
    refused(decide("Skill", {}, "critic"), "не разрешён")


# --- исследователь: чтение ---


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_researcher_reads_sources_and_knowledge(tool):
    key = "file_path" if tool == "Read" else "path"
    for target in (
        "projects/demo/sources/page.txt",
        "projects/demo/sources",
        "knowledge/roles/researcher.md",
    ):
        tin = {key: target}
        if tool == "Grep":
            tin["pattern"] = "цена"
        if tool == "Glob":
            tin["pattern"] = "*.txt"
        allowed(decide(tool, tin, "researcher"))


@pytest.mark.parametrize(
    "target",
    [
        "projects/demo/inputs/a.txt",
        "projects/demo/outputs/frame.md",
        "projects/demo/claims.yaml",
        "projects/demo/.tmp/c.yaml",
        "projects/demo",
        "projects",
        "docs/architecture.md",
        "README.md",
        "src/strateg/cli.py",
        "AGENTS.md",
        "projects/demo/sources/../inputs/a.txt",
        "projects/demo/sources/../../../.env",
        "/etc/passwd",
        "../outside.txt",
    ],
)
def test_researcher_cannot_read_elsewhere(target):
    refused(read(target, "researcher"))


def test_researcher_grep_and_glob_without_path_refused():
    refused(decide("Grep", {"pattern": "x"}, "researcher"), "path")
    refused(decide("Glob", {"pattern": "**/*.md"}, "researcher"), "path")


@pytest.mark.parametrize(
    "tin",
    [
        {"pattern": "../../inputs/*", "path": "projects/demo/sources"},
        {"pattern": "/etc/*", "path": "projects/demo/sources"},
        {"pattern": "/home/ivan/strateg agent/projects/demo/inputs/*.txt", "path": "projects/demo/sources"},
        {"pattern": "~/.strateg/**"},
    ],
)
def test_researcher_glob_pattern_cannot_leave_sources(tin):
    refused(decide("Glob", tin, "researcher"))


def test_windows_backslash_and_case_bypasses_for_researcher():
    root = WIN_ROOT
    refused(read(root + r"\projects\demo\sources\..\inputs\a.txt", "researcher", NT))
    refused(read(r"projects\demo\sources\..\inputs\a.txt", "researcher", NT))
    refused(read(r"..\inputs\a.txt", "researcher", NT, cwd=root + r"\projects\demo\sources"))
    refused(read(r"Projects\Demo\Sources\..\INPUTS\a.txt".replace("Demo", "demo"), "researcher", NT))
    allowed(read(r"Projects\demo\SOURCES\page.txt", "researcher", NT))
    allowed(read(root.upper() + r"\PROJECTS\demo\sources\page.txt", "researcher", NT))


def test_case_bypass_per_os():
    # `Sources/../inputs`: после приведения это inputs, регистр не важен ни на одной ОС
    for penv in (NT, LINUX, MAC):
        refused(read("projects/demo/Sources/../inputs/a.txt", "researcher", penv))
    # `INPUTS/` на Windows и macOS — тот же каталог, закрыт; на Linux это другой, но и он вне sources/
    for penv in (NT, LINUX, MAC):
        refused(read("projects/demo/INPUTS/a.txt", "researcher", penv))
    # `Sources/` на Windows и macOS — тот же каталог, открыт; на Linux другое имя, отказ
    allowed(read("projects/demo/Sources/a.txt", "researcher", NT))
    allowed(read("projects/demo/Sources/a.txt", "researcher", MAC))
    refused(read("projects/demo/Sources/a.txt", "researcher", LINUX))


def test_home_and_variable_bypasses():
    refused(read("~/.strateg/private/demo/И-1/a.txt", "researcher"))
    refused(read("~/.strateg/private/demo/И-1/a.txt"), ".strateg")
    refused(read(r"%USERPROFILE%\.strateg\private\x", "researcher", NT))
    refused(read(r"%USERPROFILE%\.strateg\private\x", None, NT), ".strateg")
    refused(read("$HOME/.strateg/x", None, LINUX), ".strateg")
    refused(read("$env:USERPROFILE/.strateg/x", None, NT), ".strateg")
    refused(read(WIN_HOME + r"\.strateg\x", None, NT), ".strateg")
    refused(read("$UNKNOWN_VARIABLE/x", None, LINUX), "не удалось привести")
    refused(read(r"%UNKNOWN%\x", "critic", NT), "не удалось привести")


def test_symlink_into_secret_zone_refused_after_real_path():
    import dataclasses

    from test_hooks_paths import fake_realpath

    penv = dataclasses.replace(
        LINUX,
        realpath=fake_realpath(
            {"/home/ivan/strateg agent/projects/demo/sources/link": "/home/ivan/.strateg/private"}
        ),
    )
    refused(read("projects/demo/sources/link/x.txt", "researcher", penv))
    refused(read("projects/demo/sources/link/x.txt", None, penv), ".strateg")


# --- критик ---


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_critic_reads_project_folder_and_knowledge(tool):
    key = "file_path" if tool == "Read" else "path"
    for target in (
        "projects/demo/claims.yaml",
        "projects/demo/options.yaml",
        "projects/demo/outputs/options.md",
        "projects/demo/sources/page.txt",
        "knowledge/methods/objections-and-premortem.md",
    ):
        tin = {key: target}
        if tool == "Grep":
            tin["pattern"] = "x"
        if tool == "Glob":
            tin["pattern"] = "*.yaml"
        allowed(decide(tool, tin, "critic"))


@pytest.mark.parametrize(
    "target", ["README.md", "docs/architecture.md", "src/strateg/cli.py", "AGENTS.md", "/etc/hosts"]
)
def test_critic_cannot_read_outside_project_and_knowledge(target):
    refused(read(target, "critic"), "Критик читает")


def test_critic_has_nothing_but_reading():
    refused(decide("Bash", {"command": "ls"}, "critic"), "Критику закрыто")
    refused(decide("Bash", {"command": "uv run strateg fetch 'https://x.com'"}, "critic"), "Критику закрыто")
    refused(decide("PowerShell", {"command": "ls"}, "critic"))
    refused(decide("Write", {"file_path": "projects/demo/outputs/a.md", "content": "x"}, "critic"), "запись")
    refused(decide("Edit", {"file_path": "projects/demo/claims.yaml"}, "critic"), "запись")
    refused(decide("WebSearch", {"query": "x"}, "critic"))
    refused(decide("Agent", {"subagent_type": "researcher", "prompt": "x"}, "critic"))
    refused(decide("mcp__playwright__browser_navigate", {"url": "https://x.com"}, "critic"))


def test_critic_grep_without_path_is_refused_because_working_folder_is_repository_root():
    refused(decide("Grep", {"pattern": "x"}, "critic"), "path")


# --- чувствительное: все роли ---


@pytest.mark.parametrize("agent", [None, "researcher", "critic", "unknown-role"])
@pytest.mark.parametrize(
    "target,name",
    [
        ("projects/demo/.tmp/pending/source.txt", ".tmp/pending/"),
        ("projects/x/.tmp/pending", ".tmp/pending/"),
        ("evals/cases/a.yaml", "evals/"),
        ("evals", "evals/"),
        (".env", ".env"),
        ("projects/demo/.env.production", ".env"),
    ],
)
def test_every_role_is_refused_sensitive_reads(agent, target, name):
    refused(read(target, agent), name)


def test_pending_read_is_refused_for_all_tools_and_via_dotdot():
    for tool, key in (("Read", "file_path"), ("Grep", "path"), ("Glob", "path")):
        refused(decide(tool, {key: "projects/demo/.tmp/pending", "pattern": "*"}), ".tmp/pending/")
    refused(read("projects/demo/inputs/../.tmp/pending/x.txt"), ".tmp/pending/")
    refused(read(r"projects\demo\.TMP\Pending\x.txt", None, NT), ".tmp/pending/")
    refused(read("projects/demo/.TMP/PENDING/x.txt", None, MAC), ".tmp/pending/")
    allowed(read("projects/demo/.tmp/frame.yaml"))


def test_env_example_stays_readable():
    allowed(read(".env.example"))


def test_evals_closed_even_for_glob_pattern():
    refused(decide("Glob", {"pattern": "evals/**/*.yaml"}), "evals/")
    refused(decide("Glob", {"pattern": "../evals/*", "path": "docs"}), "evals/")


def test_writes_to_sensitive_zones_are_refused_too():
    refused(decide("Write", {"file_path": ".env", "content": "K=1"}), ".env")
    refused(
        decide("Write", {"file_path": "projects/demo/.tmp/pending/a.txt", "content": "x"}), ".tmp/pending/"
    )


# --- основной агент: файлы ---


def test_main_agent_works_in_the_working_folder():
    for tool, tin in (
        ("Read", {"file_path": "README.md"}),
        ("Read", {"file_path": "projects/demo/inputs/a.txt"}),
        ("Read", {"file_path": "knowledge/methods/x.md"}),
        ("Read", {"file_path": "docs/architecture.md"}),
        ("Write", {"file_path": "projects/demo/outputs/frame.md", "content": "x"}),
        ("Write", {"file_path": "projects/demo/.tmp/claim.yaml", "content": "x"}),
        ("Edit", {"file_path": "projects/demo/outputs/frame.md", "old_string": "a", "new_string": "b"}),
        ("Grep", {"pattern": "x", "path": "knowledge"}),
        ("Grep", {"pattern": "x", "path": "projects/demo/outputs"}),
        ("Glob", {"pattern": "**/*.md", "path": "knowledge"}),
    ):
        allowed(decide(tool, tin))


def test_main_agent_reads_claude_code_service_folders_but_not_writes():
    allowed(read("/home/ivan/.claude/projects/x/tool-results/out.txt"))
    allowed(read(WIN_HOME + r"\.claude\projects\x\tool-results\out.txt", None, NT))
    refused(read("/home/ivan/.claude/.credentials.json"), "вне рабочей папки")
    refused(
        decide("Write", {"file_path": "/home/ivan/.claude/projects/x/a.txt", "content": "x"}), "вне рабочей"
    )


def test_main_agent_session_scratchpad_is_open():
    import dataclasses

    penv = dataclasses.replace(LINUX, scratchpad="/tmp/claude/s1/scratchpad")
    allowed(decide("Write", {"file_path": "/tmp/claude/s1/scratchpad/n.txt", "content": "x"}, penv=penv))
    refused(decide("Write", {"file_path": "/tmp/claude/other/n.txt", "content": "x"}, penv=penv))


@pytest.mark.parametrize(
    "path,penv",
    [
        ("/etc/passwd", LINUX),
        ("../other-repo/a.txt", LINUX),
        ("/home/ivan/Documents/plan.docx", LINUX),
        (r"C:\Windows\win.ini", NT),
        (r"D:\work\a.txt", NT),
        (r"\\server\share\a.txt", NT),
        (r"..\..\Documents\a.txt", NT),
        ("/Users/ivan/Documents/a.txt", MAC),
    ],
)
def test_main_agent_cannot_read_outside_working_folder(path, penv):
    refused(read(path, None, penv), "вне рабочей папки", "strateg add")


# --- состояние проекта — только CLI ---


@pytest.mark.parametrize(
    "rel",
    [
        "claims.yaml",
        "state.yaml",
        "journal.jsonl",
        "cost.jsonl",
        "sources.yaml",
        "frame.yaml",
        "plan.yaml",
        "gaps.yaml",
        "options.yaml",
        "promises.yaml",
        "verification.yaml",
        "decisions.yaml",
        "completeness.yaml",
        "other-output.yaml",
        "history/frame.v1.yaml",
        "history/outputs/options.v2.md",
        "sources/page.txt",
        "sources/и-1.txt",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_main_agent_cannot_write_cli_state_files(tool, rel):
    tin = {"file_path": f"projects/demo/{rel}", "content": "x", "old_string": "a", "new_string": "b"}
    refused(decide(tool, tin), "только CLI", "strateg")


def test_cli_state_hint_names_the_command():
    assert "claim add" in refused(decide("Edit", {"file_path": "projects/demo/claims.yaml"})).reason
    assert "strateg write" in refused(decide("Write", {"file_path": "projects/demo/plan.yaml"})).reason
    assert "fetch" in refused(decide("Write", {"file_path": "projects/demo/sources/a.txt"})).reason


def test_state_file_guard_survives_case_and_backslash():
    refused(
        decide("Write", {"file_path": r"projects\demo\Claims.YAML", "content": "x"}, penv=NT), "только CLI"
    )
    refused(
        decide("Write", {"file_path": "projects/demo/SOURCES/a.txt", "content": "x"}, penv=MAC), "только CLI"
    )
    refused(
        decide("Write", {"file_path": "projects/demo/inputs/../claims.yaml", "content": "x"}), "только CLI"
    )


def test_documents_and_command_inputs_are_written_by_the_agent():
    for rel in (
        "outputs/frame.md",
        "outputs/options.md",
        ".tmp/claim.yaml",
        ".tmp/write.yaml",
    ):
        allowed(decide("Write", {"file_path": f"projects/demo/{rel}", "content": "x"}))


def test_read_of_state_files_is_open_for_main_agent():
    for rel in ("claims.yaml", "state.yaml", "journal.jsonl", "sources/a.txt"):
        allowed(read(f"projects/demo/{rel}"))


# --- охрана правил и STRATEG_DEV ---


@pytest.mark.parametrize(
    "target",
    [
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/settings.json",
        ".claude/hooks/pre_tool_use.py",
        ".claude/agents/critic.md",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_rules_files_are_guarded(tool, target):
    tin = {"file_path": target, "content": "x", "old_string": "a", "new_string": "b"}
    refused(decide(tool, tin), "STRATEG_DEV")
    allowed(decide(tool, tin, env={"STRATEG_DEV": "1"}))


def test_dev_variable_only_lifts_rules_guard():
    dev = {"STRATEG_DEV": "1"}
    refused(decide("Write", {"file_path": ".env", "content": "x"}, env=dev), ".env")
    refused(read("evals/a.yaml", env=dev), "evals/")
    refused(read("projects/demo/.tmp/pending/a", env=dev), ".tmp/pending/")
    refused(read("~/.strateg/x", env=dev), ".strateg")
    refused(decide("WebFetch", {"url": "https://x.com"}, env=dev))
    refused(decide("Bash", {"command": "curl https://x.com"}, env=dev))
    refused(decide("Agent", {"subagent_type": "general-purpose", "prompt": "x"}, env=dev))
    refused(
        decide("Write", {"file_path": "projects/demo/claims.yaml", "content": "x"}, env=dev), "только CLI"
    )


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_dev_variable_means_exactly_one(value):
    refused(decide("Write", {"file_path": "AGENTS.md", "content": "x"}, env={"STRATEG_DEV": value}))


def test_rules_files_can_be_read_by_all():
    allowed(read("AGENTS.md"))
    allowed(read(".claude/skills/strateg/SKILL.md"))


def test_roles_cannot_edit_rules_even_with_dev():
    refused(decide("Edit", {"file_path": "AGENTS.md"}, "critic", env={"STRATEG_DEV": "1"}), "запись")


# --- Bash основного агента ---


def test_main_agent_may_run_cli_commands_except_fetch():
    for command in (
        "uv run strateg status --brief",
        "uv run strateg step start 2б",
        "uv run strateg claim add --file projects/demo/.tmp/c.yaml",
        "uv run strateg stop --reason 'curl и wget лежат'",
        "uv run pytest -q",
        "ls projects",
        "git status",
    ):
        allowed(decide("Bash", {"command": command}))
    refused(decide("Bash", {"command": "uv run strateg fetch 'https://example.com'"}), "fetch", "researcher")


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_main_agent_network_commands_refused(tool):
    for command in (
        "curl https://example.com",
        "wget https://example.com",
        "Invoke-WebRequest https://example.com",
        "iwr https://example.com",
        "uv run strateg status; curl https://example.com",
        'uv run python -c "import urllib.request"',
    ):
        refused(decide(tool, {"command": command}), "Сеть закрыта", "исследовател")


def test_main_agent_bash_reading_secrets_refused():
    refused(decide("Bash", {"command": "cat .env"}), ".env")
    refused(decide("Bash", {"command": "ls ~/.strateg/private"}), ".strateg")
    refused(decide("Bash", {"command": "cat projects/demo/.tmp/pending/a.txt"}), ".tmp/pending/")


def test_monitor_with_websocket_refused_for_main():
    refused(decide("Monitor", {"ws": {"url": "wss://x.com"}, "description": "d"}), "WebSocket")
    allowed(decide("Monitor", {"command": "tail -f projects/demo/journal.jsonl", "description": "d"}))
    refused(decide("Monitor", {"command": "curl -N https://x.com", "description": "d"}), "curl")


def test_bash_without_command_is_refused():
    refused(decide("Bash", {}), "нет команды")


# --- Playwright (ADR-0013) ---


NAV = "mcp__playwright__browser_navigate"
SHOT = "mcp__playwright__browser_take_screenshot"


def test_playwright_closed_for_main_agent():
    for name in (
        NAV,
        SHOT,
        "mcp__playwright__browser_click",
        "mcp__plugin_playwright_playwright__browser_navigate",
    ):
        refused(decide(name, {"url": "https://x.com"}), "Браузер закрыт")


def test_researcher_playwright_whitelist():
    allowed(decide(NAV, {"url": "https://x.com/app"}, "researcher"))
    allowed(decide(SHOT, {}, "researcher"))
    allowed(decide(SHOT, {"fullPage": True}, "researcher"))
    allowed(
        decide("mcp__plugin_playwright_playwright__browser_navigate", {"url": "http://x.com"}, "researcher")
    )


@pytest.mark.parametrize(
    "tool",
    [
        "browser_click",
        "browser_type",
        "browser_snapshot",
        "browser_evaluate",
        "browser_run_code_unsafe",
        "browser_file_upload",
        "browser_fill_form",
        "browser_press_key",
        "browser_navigate_back",
        "browser_tabs",
        "browser_console_messages",
        "browser_network_requests",
        "some_other_tool",
    ],
)
def test_researcher_other_playwright_tools_refused(tool):
    refused(decide(f"mcp__playwright__{tool}", {}, "researcher"), "только `browser_navigate`")


def test_screenshot_filename_is_refused():
    refused(decide(SHOT, {"filename": "../../.env"}, "researcher"), "filename")
    refused(decide(SHOT, {"filename": ""}, "researcher"), "filename")


@pytest.mark.parametrize(
    "url", ["file:///C:/Windows/win.ini", "javascript:alert(1)", "about:blank", "chrome://settings", "x.com"]
)
def test_navigate_only_http(url):
    refused(decide(NAV, {"url": url}, "researcher"), "http")


# --- запуск субагента: допустимые роли и модель ---


def launch(role="critic", prompt="режим: полнота; папка projects/demo", view=VIEW, **kw):
    return decide(
        "Agent", {"subagent_type": role, "prompt": prompt, "description": "d"}, svc=services(view, **kw)
    )


def test_role_launch_substitutes_model_from_routing_and_writes_one_event():
    d = allowed(launch("critic"))
    assert d.updated_input["model"] == "sonnet"
    assert d.updated_input["subagent_type"] == "critic" and d.updated_input["prompt"].startswith("режим")
    assert d.updated_input["description"] == "d"  # остальные поля вызова не теряются
    picks = [e for e in d.events if e.kind == "выбор модели"]
    assert len(picks) == 1
    e = picks[0]
    assert (e.role, e.model, e.level, e.effort, e.name) == ("critic", "sonnet", "средняя", "high", "выбор")
    assert "route не вызван" in e.message
    out = d.output()
    assert '"permissionDecision": "allow"' in out and '"model": "sonnet"' in out


def test_route_event_exists_its_model_is_used_and_not_duplicated():
    view = ProjectView(
        "demo", "2б", "обычный", picks=(Pick("critic", "opus", "2б", "повышение"),), plan_ids=frozenset()
    )
    d = allowed(launch("critic", view=view))
    assert d.updated_input["model"] == "opus"  # повышение (`route --upgrade`) учтено
    assert not [e for e in d.events if e.kind == "выбор модели"]


def test_last_route_event_wins():
    picks = (Pick("critic", "sonnet", "2б", "выбор"), Pick("critic", "opus", "2б", "повышение"))
    d = allowed(launch("critic", view=ProjectView("demo", "2б", "обычный", picks=picks)))
    assert d.updated_input["model"] == "opus"


def test_route_event_of_other_step_or_role_does_not_count():
    picks = (Pick("critic", "opus", "4", "повышение"), Pick("researcher", "haiku", "2б", "выбор"))
    d = allowed(launch("critic", view=ProjectView("demo", "2б", "обычный", picks=picks)))
    assert d.updated_input["model"] == "sonnet"
    assert len([e for e in d.events if e.kind == "выбор модели"]) == 1


def test_model_follows_profile_in_routing_view():
    deep = RoutingView(roles={**ROUTING.roles, "critic": Choice("сильная", "opus", "high")})
    d = allowed(launch("critic", routing=deep, view=ProjectView("demo", "2б", "глубже")))
    assert d.updated_input["model"] == "opus"
    assert "профиль глубже" in [e for e in d.events if e.kind == "выбор модели"][0].message


def test_launch_without_active_project_uses_routing_and_writes_nothing_to_choose():
    d = decide("Agent", {"subagent_type": "critic", "prompt": "режим полнота"}, svc=services(view=None))
    allowed(d)
    assert d.updated_input["model"] == "sonnet" and not d.events


def test_unknown_role_is_refused_with_clear_reason():
    refused(launch("strategist"), "Неизвестная роль", "researcher")
    refused(launch("plugin:researcher"), "Неизвестная роль")


def test_role_without_wrapper_is_refused_clearly():
    d = refused(launch("classifier"), "нет обёртки", "запасному пути")
    assert "AGENTS.md" in d.reason
    refused(launch("respondent"), "нет обёртки")


def test_role_without_routing_key_is_refused_clearly():
    wrappers = frozenset({"researcher", "critic", "analyst"})
    refused(launch("analyst", wrappers=wrappers), "нет в `config/routing.yaml`", "researcher")


def test_routing_table_error_refuses_with_its_text():
    def broken():
        raise rules.HookError("таблица маршрутов не читается: нет файла")

    svc = Services(project=lambda: VIEW, routing=broken, agents=lambda: WRAPPERS)
    refused(
        decide("Agent", {"subagent_type": "critic", "prompt": "x"}, svc=svc), "таблица маршрутов не читается"
    )


def test_model_that_is_not_an_alias_is_refused():
    bad = RoutingView(roles={**ROUTING.roles, "critic": Choice("средняя", "claude-sonnet-5-5", "high")})
    refused(launch("critic", routing=bad), "не псевдоним")


# --- задание исследователю ---


def test_researcher_task_is_logged_and_passes_checks():
    d = allowed(
        launch("researcher", "Вопрос ВП-2: цена подписки; предмет: тариф; период 2026; регион RU-MOW")
    )
    tasks = [e for e in d.events if e.kind == "задание исследователю"]
    assert len(tasks) == 1 and "ВП-2" in tasks[0].message and tasks[0].role == "researcher"
    assert d.updated_input["model"] == "sonnet"


def test_researcher_task_without_plan_number_is_fine():
    allowed(launch("researcher", "Страница: https://example.com/tariff; регион RU; нужно: снимок"))


def test_researcher_task_with_missing_plan_question_refused():
    d = refused(launch("researcher", "Вопрос ВП-7: цена"), "ВП-7", "plan.yaml")
    assert not [e for e in d.events if e.kind == "задание исследователю"]
    refused(launch("researcher", "Вопрос ВП-1 и ВП-9"), "ВП-9")
    refused(launch("researcher", "ВП-01: цена"), "ВП-01")
    refused(launch("researcher", "ВП–7: цена"), "ВП-7")  # длинное тире


def test_researcher_task_without_active_project_cannot_name_plan_questions():
    refused(launch("researcher", "ВП-1: цена", view=None), "ВП-1")


@pytest.mark.parametrize(
    "prompt",
    [
        "Прочитай projects/demo/inputs/brief.txt и найди цену",
        "см. inputs/notes.md",
        r"см. projects\demo\outputs\frame.md",
        "возьми цифры из outputs/situation.md",
        "OUTPUTS/frame.md",
    ],
)
def test_researcher_task_with_project_paths_refused(prompt):
    refused(launch("researcher", prompt), "inputs/", "Передай только вопрос")


def test_empty_researcher_task_refused():
    refused(launch("researcher", "   "), "пустое")


def test_critic_task_is_not_checked_against_plan():
    d = allowed(launch("critic", "режим: возражения; ВП-9; projects/demo/outputs/options.md"))
    assert not [e for e in d.events if e.kind == "задание исследователю"]


def test_task_check_function_directly():
    assert rules.check_task("ВП-1", frozenset({"ВП-1"})) is None
    assert "ВП-2" in rules.check_task("ВП-2", frozenset({"ВП-1"}))
    assert rules.check_task("текст без номеров", frozenset()) is None


# --- вход не Claude Code ---


def foreign(tool, tin=None, **extra):
    data = {
        "hook_event_name": "preToolUse",
        "conversation_id": "c-1",
        "cursor_version": "3.0",
        "tool_name": tool,
    }
    data.update({"tool_input": tin or {}, "cwd": LINUX.cwd, **extra})
    return decide_pre_tool_use(data, Ctx(penv=LINUX, env={}), services())


@pytest.mark.parametrize(
    "data",
    [
        {"tool_name": "Bash", "tool_input": {}},  # нет hook_event_name и session_id
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},  # нет session_id
        {"session_id": "s", "tool_name": "Bash", "tool_input": {}},  # нет hook_event_name
        {
            "hook_event_name": "preToolUse",
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {},
        },  # camelCase
        {"hook_event_name": "PreToolUse", "session_id": "s", "cursor_version": "3", "tool_name": "Bash"},
        {"hook_event_name": "PreToolUse", "session_id": "s", "workspace_roots": ["/x"], "tool_name": "Bash"},
        {"hook_event_name": "PreToolUse", "session_id": "", "tool_name": "Bash"},
    ],
)
def test_not_claude_code_inputs_are_recognized(data):
    assert not rules.is_claude_code(data)


def test_claude_code_input_is_recognized():
    assert rules.is_claude_code(event("Bash"))


def test_role_rules_do_not_apply_outside_claude_code_and_event_is_written():
    d = foreign("WebFetch", {"url": "https://x.com"})
    assert not d.denied  # правила по ролям не применяются
    notes = [e for e in d.events if e.kind == "граница прав не активна"]
    assert len(notes) == 1 and notes[0].name == "без номера сессии"
    d = foreign("Bash", {"command": "curl https://x.com"}, agent_type="researcher")
    assert not d.denied
    d = foreign("Agent", {"subagent_type": "general-purpose"})
    assert not d.denied


def test_note_uses_session_id_when_present():
    d = decide_pre_tool_use(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}, "session_id": ""},
        Ctx(penv=LINUX, env={}),
        services(),
    )
    assert d.events[0].name == "без номера сессии"
    d = decide_pre_tool_use(
        {"hook_event_name": "preToolUse", "session_id": "s-77", "tool_name": "Bash", "tool_input": {}},
        Ctx(penv=LINUX, env={}),
        services(),
    )
    assert d.events[0].name == "s-77"


def test_guard_works_outside_claude_code():
    refused(foreign("Read", {"file_path": ".env"}), ".env")
    refused(foreign("Read", {"file_path": "~/.strateg/private/x"}), ".strateg")
    refused(foreign("Read", {"file_path": "projects/demo/.tmp/pending/a.txt"}), ".tmp/pending/")
    refused(foreign("Write", {"file_path": "AGENTS.md", "content": "x"}), "Правила агента")
    refused(foreign("Edit", {"file_path": ".claude/settings.json"}), "Правила агента")
    refused(foreign("Shell", {"command": "cat .env"}), ".env")
    allowed(foreign("Write", {"file_path": "AGENTS.md", "content": "x"}, tool_input={"file_path": "x"}))


def test_guard_outside_claude_code_respects_dev_variable():
    data = {"hook_event_name": "preToolUse", "tool_name": "Write", "cursor_version": "3"}
    data["tool_input"] = {"file_path": "AGENTS.md", "content": "x"}
    d = decide_pre_tool_use(data, Ctx(penv=LINUX, env={"STRATEG_DEV": "1"}), services())
    assert not d.denied
    d = decide_pre_tool_use(
        {**data, "tool_input": {"file_path": ".env"}}, Ctx(penv=LINUX, env={"STRATEG_DEV": "1"}), services()
    )
    assert d.denied


def test_evals_are_not_closed_without_roles():
    assert not foreign("Read", {"file_path": "evals/a.yaml"}).denied


# --- вывод хука ---


def test_deny_output_is_one_json_object_with_reason():
    import json

    d = decide("WebFetch", {"url": "https://x.com"})
    body = json.loads(d.output())["hookSpecificOutput"]
    assert body["hookEventName"] == "PreToolUse" and body["permissionDecision"] == "deny"
    assert "WebFetch" in body["permissionDecisionReason"]


def test_plain_allow_prints_nothing():
    assert decide("Read", {"file_path": "README.md"}).output() == ""


def test_reasons_are_short_russian_text():
    for d in (
        decide("WebFetch", {"url": "https://x.com"}),
        read("projects/demo/inputs/a.txt", "researcher"),
        decide("Bash", {"command": "curl https://x.com"}),
        decide("Write", {"file_path": "projects/demo/claims.yaml"}),
        launch("classifier"),
    ):
        assert d.denied and len(d.reason) < 400
        assert any("а" <= c.lower() <= "я" for c in d.reason)
        assert d.reason.count("\n") <= 1


def test_refusal_event_carries_tool_and_role():
    d = decide("Bash", {"command": "curl https://x.com"}, "researcher")
    e = [x for x in d.events if x.kind == "отказ хука"][0]
    assert e.name == "Bash" and e.role == "researcher"
    d = decide("WebFetch", {"url": "https://x.com"})
    assert [x for x in d.events if x.kind == "отказ хука"][0].role is None


def test_unknown_agent_type_gets_main_agent_rights():
    allowed(decide("Read", {"file_path": "projects/demo/inputs/a.txt"}, "some-other-agent"))
    refused(decide("WebSearch", {"query": "x"}, "some-other-agent"))
    refused(decide("Bash", {"command": "curl x"}, "some-other-agent"))
    allowed(decide("Agent", {"subagent_type": "critic", "prompt": "x"}, "some-other-agent"))


def test_agent_id_without_agent_type_is_not_main_agent_but_has_its_rights():
    e = event("Read", {"file_path": "README.md"})
    e["agent_id"] = "a-2"
    assert rules.role_kind(e) == rules.OTHER


def test_main_agent_other_tools_pass():
    allowed(decide("TodoWrite", {}))
    allowed(decide("mcp__other__thing", {}))


def test_cli_state_files_match_project_check():
    from strateg import project_check as pc

    expected = {
        pc.STATE_FILE,
        pc.SOURCES_FILE,
        pc.CLAIMS_FILE,
        pc.JOURNAL_FILE,
        pc.COST_FILE,
        *pc.TRACKED_OUTPUTS,
    }
    assert set(rules.CLI_STATE_FILES) == expected
