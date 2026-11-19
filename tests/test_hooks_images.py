"""Изображение читают только после `add --confirm`: правило хука прав и вопрос человеку (ADR-0011).

Хук запускается настоящим скриптом, как в Claude Code: JSON на stdin, ответ на stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strateg import materials
from strateg.hooks import rules
from strateg.hooks.rules import Ctx, Services, decide_pre_tool_use
from test_hooks_integration import SETTINGS, pre_event, run_hook
from test_hooks_integration import product as _product_fixture
from test_hooks_rules import LINUX, event
from test_image_claims import add_image, png_bytes

product = _product_fixture  # фикстура копии продукта из test_hooks_integration


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def read_image(product, home: Path, path: Path, agent: str | None = None) -> tuple[bool, str]:
    extra = {"agent_type": agent, "agent_id": "a-1"} if agent else {}
    ev = pre_event("Read", {"file_path": str(path)}, **extra)
    proc = run_hook(product, "pre_tool_use.py", ev, STRATEG_PROJECTS_DIR=str(home))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    text = proc.stdout.decode("utf-8").strip()
    if not text:
        return False, ""
    out = json.loads(text)["hookSpecificOutput"]
    return out["permissionDecision"] == "deny", out.get("permissionDecisionReason", "")


def test_main_agent_cannot_read_an_image_until_the_human_confirms(demo, cli, home, product):
    add_image(demo, cli, confirm=False)
    image = demo / "inputs" / "оплата.png"
    denied, why = read_image(product, home, image)
    assert denied and "add --confirm" in why and "оплата.png" in why
    assert cli("add", "--confirm", "И-1")[0] == 0
    assert read_image(product, home, image) == (False, "")


def test_image_that_was_never_registered_is_closed_too(demo, cli, home, product):
    (demo / "inputs").mkdir(exist_ok=True)
    stray = demo / "inputs" / "снимок.PNG"  # заглавное расширение не обходит правило
    stray.write_bytes(png_bytes(4))
    denied, _ = read_image(product, home, stray)
    assert denied


def test_image_replaced_after_confirmation_is_closed_again(demo, cli, home, product):
    add_image(demo, cli)
    image = demo / "inputs" / "оплата.png"
    assert read_image(product, home, image) == (False, "")
    image.write_bytes(png_bytes(8))
    assert read_image(product, home, image)[0]


def test_the_rule_applies_to_the_roles_and_spares_outputs_and_text(demo, cli, home, product):
    add_image(demo, cli, confirm=False)
    (demo / "outputs").mkdir(exist_ok=True)
    chart = demo / "outputs" / "график.png"
    chart.write_bytes(png_bytes(2))
    assert read_image(product, home, chart) == (False, "")  # свой рисунок агента
    note = demo / "inputs" / "заметка.txt"
    note.write_text("текст", encoding="utf-8")
    assert read_image(product, home, note) == (False, "")
    image = demo / "inputs" / "оплата.png"
    for role in ("researcher", "critic"):
        denied, _ = read_image(product, home, image, agent=role)
        assert denied, role


def services(confirmed: bool, active: str | None = "demo") -> Services:
    return Services(
        project=lambda: None,
        routing=lambda: (_ for _ in ()).throw(AssertionError("таблица маршрутов не нужна")),
        agents=lambda: frozenset(),
        active=lambda: active,
        image_confirmed=lambda rel: confirmed,
    )


def read(path: str, svc: Services):
    return decide_pre_tool_use(event("Read", {"file_path": path}), Ctx(penv=LINUX, env={}), svc)


def test_decision_depends_on_the_confirmation_of_the_active_project():
    path = "projects/demo/inputs/оплата.png"
    assert read(path, services(False)).denied
    assert not read(path, services(True)).denied
    assert read(path, services(True, active="other")).denied  # источник сверяется с активным проектом
    assert read(path, services(True, active=None)).denied


def test_default_services_trust_nothing():
    svc = Services(project=lambda: None, routing=lambda: None, agents=lambda: frozenset())  # type: ignore[arg-type,return-value]
    assert svc.image_confirmed(("inputs", "a.png")) is False


def test_hook_closes_every_kind_that_add_accepts():
    assert rules.IMAGE_SUFFIXES >= materials.IMAGES


def test_claude_code_asks_the_human_before_add_confirm():
    ask = SETTINGS["permissions"]["ask"]
    assert ask == ["Bash(uv run strateg add --confirm*)"]
    assert not any("add --confirm" in rule for rule in SETTINGS["permissions"].get("allow", []))
