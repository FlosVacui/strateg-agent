"""Права по зонам: критик, активный проект, поиск основного агента, `..` в шаблоне, запись, имя роли."""

from __future__ import annotations

import dataclasses

import pytest

from strateg.hooks.rules import Services, decide_pre_tool_use
from test_hooks_paths import LINUX, NT
from test_hooks_rules import ROUTING, VIEW, WRAPPERS, allowed, decide, event, read, refused

DEV = {"STRATEG_DEV": "1"}


def with_active(active: str | None) -> Services:
    return Services(
        project=lambda: VIEW, routing=lambda: ROUTING, agents=lambda: WRAPPERS, active=lambda: active
    )


def keyed(tool: str, target: str, **extra) -> dict:
    key = "file_path" if tool == "Read" else "path"
    tin = {key: target, **extra}
    if tool == "Grep":
        tin["pattern"] = "x"
    if tool == "Glob":
        tin.setdefault("pattern", "*.yaml")
    return tin


# --- критик: закрыты inputs/, .tmp/, state.yaml, cost.jsonl, completeness.yaml, history/ ---

CRITIC_CLOSED = [
    "projects/demo/inputs/a.pdf",
    "projects/demo/inputs",
    "projects/demo/.tmp/draft.yaml",
    "projects/demo/.tmp",
    "projects/demo/state.yaml",
    "projects/demo/cost.jsonl",
    "projects/demo/completeness.yaml",
    "projects/demo/history/claims.v1.yaml",
    "projects/demo/history",
    "projects/demo/outputs/../inputs/a.pdf",
    "projects/demo/sources/../state.yaml",
]
CRITIC_OPEN = [
    "projects/demo/journal.jsonl",
    "projects/demo/sources/s1.txt",
    "projects/demo/sources",
    "projects/demo/outputs/options.md",
    "projects/demo/outputs",
    "projects/demo/claims.yaml",
    "projects/demo/sources.yaml",
    "projects/demo/options.yaml",
    "projects/demo/frame.yaml",
    "projects/demo/plan.yaml",
    "projects/demo/gaps.yaml",
    "projects/demo/promises.yaml",
    "projects/demo/verification.yaml",
    "knowledge/roles/critic.md",
]


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
@pytest.mark.parametrize("target", CRITIC_CLOSED)
def test_critic_cannot_open_inputs_tmp_and_service_files(tool, target):
    refused(decide(tool, keyed(tool, target), "critic"), "Критик")


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
@pytest.mark.parametrize("target", CRITIC_OPEN)
def test_critic_reads_conclusions_journal_and_snapshots(tool, target):
    allowed(decide(tool, keyed(tool, target), "critic"))


def test_critic_grep_over_the_whole_project_folder_is_refused_with_a_hint():
    d = refused(decide("Grep", {"pattern": "x", "path": "projects/demo"}, "critic"), "Критик")
    assert "outputs" in d.reason


@pytest.mark.parametrize(
    "pattern", ["**/*.pdf", "*/*.pdf", "inputs/*", "**/state.yaml", ".tmp/*", "history/**"]
)
def test_critic_glob_from_the_project_root_cannot_reach_closed_folders(pattern):
    refused(decide("Glob", {"pattern": pattern, "path": "projects/demo"}, "critic"), "Критик")


def test_critic_glob_inside_open_folders_and_flat_listing_of_the_root_are_fine():
    allowed(decide("Glob", {"pattern": "**/*.md", "path": "projects/demo/outputs"}, "critic"))
    allowed(decide("Glob", {"pattern": "outputs/*.md", "path": "projects/demo"}, "critic"))
    allowed(decide("Glob", {"pattern": "*.yaml", "path": "projects/demo"}, "critic"))


def test_critic_closed_folders_on_windows_with_backslash_and_case():
    for target in (r"projects\demo\Inputs\a.pdf", r"Projects\demo\.TMP\x", r"projects\demo\STATE.YAML"):
        refused(read(target, "critic", NT), "Критик")
    allowed(read(r"projects\demo\Journal.jsonl", "critic", NT))


# --- исследователь и критик: только папка активного проекта ---


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_researcher_reads_only_sources_of_the_active_project(tool):
    allowed(decide(tool, keyed(tool, "projects/demo/sources/s1.txt"), "researcher", svc=with_active("demo")))
    refused(
        decide(tool, keyed(tool, "projects/other/sources/s1.txt"), "researcher", svc=with_active("demo")),
        "активного проекта",
    )


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_critic_reads_only_the_active_project(tool):
    allowed(decide(tool, keyed(tool, "projects/demo/outputs/a.md"), "critic", svc=with_active("demo")))
    refused(
        decide(tool, keyed(tool, "projects/other/outputs/a.md"), "critic", svc=with_active("demo")),
        "активного проекта",
    )


def test_knowledge_stays_open_to_roles_whatever_the_active_project():
    for role in ("researcher", "critic"):
        allowed(decide("Read", {"file_path": "knowledge/roles/x.md"}, role, svc=with_active("demo")))


def test_without_an_active_project_roles_keep_the_previous_zones():
    allowed(
        decide("Read", {"file_path": "projects/other/sources/s1.txt"}, "researcher", svc=with_active(None))
    )
    refused(decide("Read", {"file_path": "projects/other/inputs/a.txt"}, "researcher", svc=with_active(None)))


# --- основной агент: Grep и Glob только с path в активном проекте или в knowledge/ ---


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_main_agent_search_without_path_is_refused_with_a_hint(tool):
    d = refused(decide(tool, {"pattern": "ориентир"}, svc=with_active("demo")), "path")
    assert "projects/<id>" in d.reason and "knowledge" in d.reason


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
@pytest.mark.parametrize(
    "target",
    [
        "projects/demo/outputs",
        "projects/demo/inputs",
        "projects/demo/sources/page.txt",
        "knowledge",
        "knowledge/methods",
    ],
)
def test_main_agent_search_in_active_project_and_knowledge(tool, target):
    allowed(decide(tool, keyed(tool, target), svc=with_active("demo")))


def test_main_agent_grep_over_the_project_root_is_refused_with_a_hint():
    for env in (None, DEV):  # `.tmp/pending/` закрыт и в режиме разработки
        d = refused(
            decide("Grep", {"pattern": "x", "path": "projects/demo"}, env=env, svc=with_active("demo")),
            "Поиск",
        )
        assert "outputs" in d.reason and "sources" in d.reason


@pytest.mark.parametrize("pattern", ["**/*.pdf", "*/*.pdf", "**/state.yaml", ".tmp/**", "**"])
def test_main_agent_recursive_glob_from_the_project_root_is_refused(pattern):
    for env in (None, DEV):
        refused(
            decide("Glob", {"pattern": pattern, "path": "projects/demo"}, env=env, svc=with_active("demo")),
            "Поиск",
        )


def test_main_agent_flat_glob_and_deeper_globs_from_the_project_root_are_fine():
    allowed(decide("Glob", {"pattern": "*.yaml", "path": "projects/demo"}, svc=with_active("demo")))
    allowed(decide("Glob", {"pattern": "outputs/**/*.md", "path": "projects/demo"}, svc=with_active("demo")))
    allowed(decide("Glob", {"pattern": "**/*.txt", "path": "projects/demo/sources"}, svc=with_active("demo")))


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
@pytest.mark.parametrize(
    "target",
    [".", "docs", "src", "tests", "README.md", "projects", "projects/other", "projects/other/outputs"],
)
def test_main_agent_search_elsewhere_is_refused(tool, target):
    refused(decide(tool, keyed(tool, target), svc=with_active("demo")), "Поиск")


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_main_agent_search_over_the_repository_root_by_absolute_path_is_refused(tool):
    refused(decide(tool, keyed(tool, LINUX.root), svc=with_active("demo")), "Поиск")
    refused(decide(tool, keyed(tool, NT.root), penv=NT, svc=with_active("demo")), "Поиск")


def test_main_agent_search_without_active_project_is_open_in_knowledge_and_any_project_folder():
    allowed(decide("Grep", keyed("Grep", "knowledge"), svc=with_active(None)))
    allowed(decide("Grep", keyed("Grep", "projects/other/outputs"), svc=with_active(None)))
    refused(decide("Grep", {"pattern": "x"}, svc=with_active(None)), "path")
    refused(decide("Grep", keyed("Grep", "docs"), svc=with_active(None)), "Поиск")


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_dev_mode_lifts_the_search_limit_of_the_main_agent(tool):
    allowed(decide(tool, {"pattern": "x"}, env=DEV, svc=with_active("demo")))
    for target in ("docs", "src", "tests", ".", "projects/other/outputs"):
        allowed(decide(tool, keyed(tool, target), env=DEV, svc=with_active("demo")))


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_dev_mode_keeps_the_closed_zones_in_search(tool):
    refused(decide(tool, keyed(tool, "evals"), env=DEV, svc=with_active("demo")), "evals/")
    refused(decide(tool, keyed(tool, ".env"), env=DEV, svc=with_active("demo")), ".env")
    refused(
        decide(tool, keyed(tool, "projects/demo/.tmp/pending"), env=DEV, svc=with_active("demo")), "pending"
    )
    refused(decide(tool, keyed(tool, "~/.strateg"), env=DEV, svc=with_active("demo")), ".strateg")
    refused(
        decide("Glob", {"pattern": "evals/**", "path": "docs"}, env=DEV, svc=with_active("demo")), "evals"
    )
    refused(decide("Glob", {"pattern": "../x/*", "path": "docs"}, env=DEV, svc=with_active("demo")), "..")


@pytest.mark.parametrize("role", ["researcher", "critic"])
def test_dev_mode_does_not_open_search_for_roles(role):
    refused(decide("Grep", {"pattern": "x"}, role, env=DEV))
    refused(decide("Grep", keyed("Grep", "docs"), role, env=DEV))


@pytest.mark.parametrize(
    "tin",
    [
        {"pattern": "evals/**/*.md", "path": "knowledge"},
        {"pattern": "**/evals/**", "path": "projects/demo"},
        {"pattern": "*/evals/*", "path": "knowledge"},
        {"pattern": "**/EVALS/cases/*", "path": "knowledge"},
        {"pattern": "{evals,docs}/*", "path": "knowledge"},
        {"pattern": "evals\\cases\\*", "path": "knowledge"},
    ],
)
def test_glob_pattern_with_evals_is_refused(tin):
    refused(decide("Glob", tin, svc=with_active("demo")), "evals")


def test_grep_glob_filter_with_evals_is_refused():
    refused(
        decide("Grep", {"pattern": "x", "path": "knowledge", "glob": "evals/**"}, svc=with_active("demo")),
        "evals",
    )
    refused(
        decide(
            "Grep", {"pattern": "x", "path": "knowledge", "glob": "**/evals/*.md"}, svc=with_active("demo")
        ),
        "evals",
    )
    allowed(decide("Grep", {"pattern": "x", "path": "knowledge", "glob": "*.md"}, svc=with_active("demo")))


def test_word_evals_in_a_grep_pattern_is_just_text():
    allowed(decide("Grep", {"pattern": "evals", "path": "knowledge"}, svc=with_active("demo")))


# --- Glob с `..` у ролей (и у основного агента) ---


@pytest.mark.parametrize(
    "pattern",
    [
        "*/../../inputs/*",
        "**/../inputs/*",
        "[.][.]/inputs/*",
        "{../../inputs,x}/*",
        "..*/inputs/*",
        "../../inputs/*",
        "..\\..\\inputs\\*",
        "a/../b",
        "sources/..",
    ],
)
@pytest.mark.parametrize("role", ["researcher", "critic"])
def test_glob_pattern_with_dotdot_is_refused_for_roles(role, pattern):
    refused(decide("Glob", {"pattern": pattern, "path": "projects/demo/sources"}, role), "..")


def test_glob_pattern_with_dotdot_is_refused_for_the_main_agent_too():
    refused(
        decide("Glob", {"pattern": "*/../../x/*", "path": "projects/demo"}, svc=with_active("demo")), ".."
    )


def test_glob_patterns_with_dots_that_are_not_parents_stay_open():
    allowed(decide("Glob", {"pattern": "**/*.md", "path": "projects/demo/sources"}, "researcher"))
    allowed(decide("Glob", {"pattern": "page.v1..txt", "path": "projects/demo/sources"}, "researcher"))
    allowed(decide("Glob", {"pattern": ".*", "path": "projects/demo/sources"}, "researcher"))


# --- имя роли в updatedInput ---


def test_updated_input_carries_the_cleaned_role_name():
    d = allowed(decide("Agent", {"subagent_type": " researcher ", "prompt": "Какова доля?", "model": "opus"}))
    assert d.updated_input["subagent_type"] == "researcher"
    assert d.updated_input["model"] == "sonnet"
    assert d.updated_input["prompt"] == "Какова доля?"


def test_updated_input_keeps_the_name_as_is_when_it_is_clean():
    d = allowed(decide("Agent", {"subagent_type": "critic", "prompt": "Режим: возражения."}))
    assert d.updated_input["subagent_type"] == "critic"


# --- запись без STRATEG_DEV=1: только outputs/ и .tmp/ активного проекта ---

WRITE_TOOLS = [
    ("Write", "file_path", {"content": "x"}),
    ("Edit", "file_path", {"old_string": "a", "new_string": "b"}),
    ("MultiEdit", "file_path", {"edits": []}),
    ("NotebookEdit", "notebook_path", {"new_source": "x"}),
]
WRITE_CLOSED = [
    "src/strateg/hooks/rules.py",
    "config/routing.yaml",
    "config/prices.yaml",
    "knowledge/roles/critic.md",
    "knowledge/methods/x.md",
    ".git/hooks/pre-commit",
    ".git/config",
    "pyproject.toml",
    "README.md",
    "docs/architecture.md",
    "tests/test_x.py",
    "schemas/x.json",
    "scripts/check_all.py",
    "projects/demo/inputs/a.txt",
    "projects/demo/draft.md",
    "projects/other/outputs/a.md",
    "projects/other/.tmp/c.yaml",
    "projects/.active",
    "projects/demo/outputs/../../../src/x.py",
]
WRITE_CLI_OWNED = [
    "projects/demo/sources/s.txt",
    "projects/demo/history/x.yaml",
    "projects/demo/outputs/../state.yaml",
    "projects/demo/claims.yaml",
    "projects/demo/journal.jsonl",
]
WRITE_OPEN = [
    "projects/demo/outputs/frame.md",
    "projects/demo/outputs/sub/run-report.md",
    "projects/demo/.tmp/claim.yaml",
    "projects/demo/.tmp/dir/x.json",
    "Projects/demo/Outputs/Frame.md",
]


@pytest.mark.parametrize("tool,key,extra", WRITE_TOOLS)
@pytest.mark.parametrize("target", WRITE_CLOSED)
def test_write_outside_outputs_and_tmp_is_refused(tool, key, extra, target):
    refused(decide(tool, {key: target, **extra}, svc=with_active("demo")), "outputs/")


@pytest.mark.parametrize("tool,key,extra", WRITE_TOOLS)
@pytest.mark.parametrize("target", WRITE_CLI_OWNED)
def test_write_of_cli_state_files_is_refused_with_the_cli_hint(tool, key, extra, target):
    refused(decide(tool, {key: target, **extra}, svc=with_active("demo")), "strateg")


@pytest.mark.parametrize("tool,key,extra", WRITE_TOOLS)
def test_write_of_metrics_is_refused_with_the_metric_add_hint(tool, key, extra):
    d = refused(
        decide(tool, {key: "projects/demo/metrics.yaml", **extra}, svc=with_active("demo")), "strateg"
    )
    assert "metric add" in d.reason


@pytest.mark.parametrize("tool,key,extra", WRITE_TOOLS)
@pytest.mark.parametrize("target", WRITE_OPEN)
def test_write_to_outputs_and_tmp_of_the_active_project_is_allowed(tool, key, extra, target):
    allowed(
        decide(
            tool,
            {key: target, **extra},
            penv=NT if target.startswith("Projects") else LINUX,
            svc=with_active("demo"),
        )
    )


@pytest.mark.parametrize("tool,key,extra", WRITE_TOOLS)
@pytest.mark.parametrize("target", ["src/strateg/hooks/rules.py", "config/routing.yaml", "knowledge/x.md"])
def test_dev_mode_lifts_the_write_zone_but_not_the_cli_state_guard(tool, key, extra, target):
    allowed(decide(tool, {key: target, **extra}, env=DEV, svc=with_active("demo")))


def test_dev_mode_keeps_the_ban_on_cli_state_files():
    refused(
        decide(
            "Write",
            {"file_path": "projects/demo/claims.yaml", "content": "x"},
            env=DEV,
            svc=with_active("demo"),
        ),
        "strateg",
    )


def test_write_without_active_project_may_target_outputs_and_tmp_of_any_project():
    allowed(decide("Write", {"file_path": "projects/x/outputs/a.md", "content": "x"}, svc=with_active(None)))
    refused(decide("Write", {"file_path": "src/x.py", "content": "x"}, svc=with_active(None)), "outputs/")


def test_scratchpad_stays_writable():
    penv = dataclasses.replace(LINUX, scratchpad="/tmp/claude/s1/scratchpad")
    allowed(
        decide(
            "Write",
            {"file_path": "/tmp/claude/s1/scratchpad/n.txt", "content": "x"},
            penv=penv,
            svc=with_active("demo"),
        )
    )


# --- запись через Bash, где это распознаётся ---

BASH_WRITE_CLOSED = [
    "echo x > src/strateg/hooks/rules.py",
    "echo x >> config/routing.yaml",
    "echo x 1> knowledge/roles/critic.md",
    "cat a | tee src/x.py",
    "cat a | tee -a knowledge/x.md",
    "sed -i 's/a/b/' config/routing.yaml",
    "sed -i.bak 's/a/b/' src/strateg/cli.py",
    "sed --in-place 's/a/b/' README.md",
    "cp a.txt src/strateg/x.py",
    "mv a.txt knowledge/x.md",
    "rm -rf src/strateg/hooks",
    "rm .git/hooks/pre-commit",
    "touch knowledge/x.md",
    "mkdir src/new",
    "ln -s a config/b",
    "Set-Content config\\routing.yaml 'x'",
    "Set-Content -Path config/routing.yaml -Value x",
    "Add-Content -Path README.md -Value x",
    "Out-File -FilePath src/x.py",
    "'x' | Out-File src/x.py",
    "New-Item -Path src/x.py",
    "Remove-Item -Recurse src",
    "Copy-Item a.txt src/x.py",
    "Move-Item a.txt knowledge/x.md",
    "echo x > projects/demo/inputs/a.txt",
    "echo x > projects/other/outputs/a.md",
    "uv run strateg status > src/x.py",
    "ls; echo x > src/x.py",
    "python -c \"open('src/x.py','w')\" > knowledge/x.md",
]
BASH_WRITE_OPEN = [
    "echo x > projects/demo/outputs/a.md",
    "cat > projects/demo/.tmp/claim.yaml <<EOF\nx: 1\nEOF",
    "echo x >> projects/demo/outputs/run-report.md",
    "echo x > /dev/null",
    "ls 2>&1 > /dev/null",
    "ls 2>/dev/null",
    "cat a | tee projects/demo/.tmp/x.txt",
    "cp a.txt projects/demo/outputs/a.txt",
    "mv projects/demo/.tmp/a.txt projects/demo/outputs/a.txt",
    "rm projects/demo/.tmp/a.txt",
    "mkdir -p projects/demo/outputs/sub",
    "Set-Content -Path projects/demo/outputs/a.md -Value x",
    "Out-File projects/demo/.tmp/c.yaml",
    "uv run strateg write --file projects/demo/.tmp/frame.yaml",
    "uv run pytest -q",
    "ls src",
    "cat src/strateg/cli.py",
    "grep -r x docs > projects/demo/.tmp/out.txt",
    "git status",
    "git diff",
    "git log --oneline",
    "uv run strateg add projects/demo/inputs/a.txt",
    "echo x > $UNKNOWN/file",
]


@pytest.mark.parametrize("command", BASH_WRITE_CLOSED)
def test_bash_write_outside_outputs_and_tmp_is_refused(command):
    refused(decide("Bash", {"command": command}, svc=with_active("demo")), "outputs/")


@pytest.mark.parametrize("command", BASH_WRITE_OPEN)
def test_bash_write_to_outputs_tmp_and_reading_commands_are_allowed(command):
    allowed(decide("Bash", {"command": command}, svc=with_active("demo")))


@pytest.mark.parametrize("command", BASH_WRITE_CLOSED[:12])
def test_bash_write_zone_is_lifted_by_dev_mode(command):
    allowed(decide("Bash", {"command": command}, env=DEV, svc=with_active("demo")))


def test_powershell_tool_has_the_same_write_zone():
    refused(
        decide("PowerShell", {"command": "Set-Content src/x.py 'x'"}, svc=with_active("demo")), "outputs/"
    )
    allowed(
        decide(
            "PowerShell", {"command": "Set-Content projects/demo/outputs/a.md 'x'"}, svc=with_active("demo")
        )
    )


def test_roles_other_than_main_are_not_affected_by_bash_write_rules_text():
    refused(decide("Bash", {"command": "echo x > a"}, "researcher"))
    refused(decide("Bash", {"command": "echo x > a"}, "critic"))


# --- evals/ в Bash основного агента: решение целиком ---


@pytest.mark.parametrize(
    "command",
    [
        "cat evals/cases/fraud-protection/expected.md",
        "git show HEAD:evals/cases/fraud-protection/expected.md",
        "git grep -n x -- evals",
        "Get-Content evals\\cases\\x",
    ],
)
def test_bash_evals_is_refused_for_the_main_agent(command):
    refused(decide("Bash", {"command": command}, svc=with_active("demo")), "evals/")


def test_foreign_environment_does_not_close_evals_in_bash_but_keeps_env_guard():
    ctx_event = {
        "hook_event_name": "preToolUse",  # не Claude Code
        "session_id": "s-1",
        "cwd": LINUX.cwd,
        "tool_name": "Bash",
        "tool_input": {"command": "cat evals/x"},
    }
    from strateg.hooks.rules import Ctx

    assert not decide_pre_tool_use(ctx_event, Ctx(penv=LINUX, env={}), with_active("demo")).denied
    ctx_event["tool_input"] = {"command": "cat .env"}
    assert decide_pre_tool_use(ctx_event, Ctx(penv=LINUX, env={}), with_active("demo")).denied


def test_event_helper_is_importable():
    assert event("Read")["tool_name"] == "Read"


# --- strateg search видит материалы человека, поэтому он только у основного агента (ADR-0011, ADR-0012) ---

SEARCH_COMMANDS = [
    'uv run strateg search "доставка"',
    "uv run strateg search 'доставка' --in материалы --json",
    "uv run strateg search доставка --also цена --in все",
]


@pytest.mark.parametrize("command", SEARCH_COMMANDS)
def test_search_is_refused_to_researcher_and_critic_and_open_to_the_main_agent(command):
    refused(decide("Bash", {"command": command}, "researcher"), "fetch")
    refused(decide("Bash", {"command": command}, "critic"), "Критику закрыто")
    allowed(decide("Bash", {"command": command}))


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_search_is_refused_to_researcher_in_any_shell_tool(tool):
    refused(decide(tool, {"command": 'uv run strateg search "доставка"'}, "researcher"))


@pytest.mark.parametrize("agent", ["researcher", "critic"])
def test_roles_cannot_read_the_search_index_with_materials_in_it(agent):
    for path in ("projects/demo/.tmp/search.sqlite", "projects/demo/.tmp"):
        refused(read(path, agent, svc=with_active("demo")))
    allowed(read("projects/demo/.tmp/search.sqlite", svc=with_active("demo")))
