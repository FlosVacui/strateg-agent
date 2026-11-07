"""Зоны ролей на настоящем проекте: активный проект определяется так же, как у CLI (`projects/.active`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from strateg import project as proj
from strateg.hooks import effects
from test_hooks_runner import env, kinds, pre

REPO = Path(proj.__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


@pytest.fixture
def two(cli, home):
    """Проекты demo (активный) и other."""
    assert cli("new", "demo")[0] == 0
    assert cli("new", "other")[0] == 0
    (home / ".active").write_text("demo", encoding="utf-8")
    return home


def p(home: Path, *parts: str) -> str:
    return str(home.joinpath(*parts))


def test_researcher_reads_sources_of_the_active_project_only(two):
    assert pre("Read", {"file_path": p(two, "demo", "sources", "s1.txt")}, "researcher") is None
    body = pre("Read", {"file_path": p(two, "other", "sources", "s1.txt")}, "researcher")
    assert body["permissionDecision"] == "deny" and "активного проекта" in body["permissionDecisionReason"]


def test_critic_reads_conclusions_of_the_active_project_only(two):
    assert pre("Read", {"file_path": p(two, "demo", "journal.jsonl")}, "critic") is None
    assert pre("Read", {"file_path": p(two, "demo", "outputs", "options.md")}, "critic") is None
    for rel in ("outputs/options.md", "claims.yaml"):
        body = pre("Read", {"file_path": p(two, "other", *rel.split("/"))}, "critic")
        assert (
            body["permissionDecision"] == "deny" and "активного проекта" in body["permissionDecisionReason"]
        )


@pytest.mark.parametrize(
    "rel",
    ["inputs/a.pdf", ".tmp/draft.yaml", "state.yaml", "cost.jsonl", "completeness.yaml", "history/x.yaml"],
)
def test_critic_cannot_read_inputs_and_service_files_of_the_active_project(two, rel):
    body = pre("Read", {"file_path": p(two, "demo", *rel.split("/"))}, "critic")
    assert body["permissionDecision"] == "deny" and "Критик" in body["permissionDecisionReason"]


def test_main_agent_search_is_limited_to_the_active_project_and_knowledge(two):
    body = pre("Grep", {"pattern": "ориентир"})
    assert body["permissionDecision"] == "deny" and "path" in body["permissionDecisionReason"]
    body = pre("Glob", {"pattern": "**/*.md"})
    assert body["permissionDecision"] == "deny" and "path" in body["permissionDecisionReason"]
    assert pre("Grep", {"pattern": "x", "path": p(two, "demo")}) is None
    assert pre("Grep", {"pattern": "x", "path": str(REPO / "knowledge")}) is None
    for path in (p(two, "other"), str(REPO / "docs"), str(REPO)):
        assert pre("Grep", {"pattern": "x", "path": path})["permissionDecision"] == "deny"
    body = pre("Glob", {"pattern": "evals/**", "path": str(REPO / "knowledge")})
    assert body["permissionDecision"] == "deny" and "evals" in body["permissionDecisionReason"]


def test_main_agent_writes_only_outputs_and_tmp_of_the_active_project(two):
    assert pre("Write", {"file_path": p(two, "demo", "outputs", "a.md"), "content": "x"}) is None
    assert pre("Write", {"file_path": p(two, "demo", ".tmp", "c.yaml"), "content": "x"}) is None
    for path in (
        p(two, "other", "outputs", "a.md"),
        p(two, "demo", "inputs", "a.txt"),
        str(REPO / "src" / "strateg" / "hooks" / "rules.py"),
        str(REPO / "config" / "routing.yaml"),
        str(REPO / "knowledge" / "roles" / "critic.md"),
    ):
        body = pre("Write", {"file_path": path, "content": "x"})
        assert body["permissionDecision"] == "deny", path
    assert (
        pre(
            "Write",
            {"file_path": str(REPO / "config" / "routing.yaml"), "content": "x"},
            e=env(STRATEG_DEV="1"),
        )
        is None
    )


def test_bash_write_outside_the_zone_is_refused_and_logged(two):
    body = pre("Bash", {"command": f"echo x > {REPO / 'src' / 'x.py'}"})
    assert body["permissionDecision"] == "deny" and "outputs/" in body["permissionDecisionReason"]
    assert kinds(two / "demo")[-1] == "отказ хука"
    assert pre("Bash", {"command": f"echo x > {p(two, 'demo', 'outputs', 'a.md')}"}) is None


def test_bash_evals_is_refused_for_the_main_agent(two):
    body = pre("Bash", {"command": "git show HEAD:evals/cases/fraud-protection/expected.md"})
    assert body["permissionDecision"] == "deny" and "evals/" in body["permissionDecisionReason"]


def test_launch_updates_the_input_with_the_cleaned_role_name(two):
    body = pre("Agent", {"subagent_type": " critic ", "prompt": "Режим: возражения."})
    assert body["permissionDecision"] == "allow"
    assert body["updatedInput"]["subagent_type"] == "critic"
