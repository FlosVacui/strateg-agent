"""Снимки экрана интерфейсов (ADR-0013): список адресов, сверка домена после перехода, `.mcp.json`, `doctor`.

Хуки запускаются настоящими скриптами, как в Claude Code: JSON на stdin, ответ на stdout. Браузер и Node
не нужны: ответы инструмента взяты из пробы сервера (`scripts/probe_playwright_mcp.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from strateg import cli as cli_module
from strateg import ux_targets as ux
from strateg.hooks import rules
from test_hooks_integration import SETTINGS, pre_event, run_hook
from test_hooks_integration import product as _product_fixture
from test_hooks_rules import decide, services
from test_image_claims import png_bytes

product = _product_fixture  # фикстура копии продукта из test_hooks_integration
ROOT = Path(__file__).resolve().parent.parent
MCP = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
NAV = "mcp__playwright__browser_navigate"
SHOT = "mcp__playwright__browser_take_screenshot"
PAGE = "http://127.0.0.1:8123/cards.html"

# Ответ `browser_navigate`, как его вернул сервер в пробе (формат текста сверен вживую).
NAV_RESPONSE = (
    "### Ran Playwright code\n```js\nawait page.goto('{url}');\n```\n### Page\n"
    "- Page URL: {final}\n- Page Title: Виртуальный помощник Ева"
)


def nav_response(url: str, final: str | None = None) -> dict:
    return {"content": [{"type": "text", "text": NAV_RESPONSE.format(url=url, final=final or url)}]}


@pytest.fixture
def demo(cli, home) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


# --- адреса ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTPS://Example.COM/App?x=1#top", "https://example.com/App?x=1"),
        ("https://example.com:443/", "https://example.com/"),
        ("http://example.com:80", "http://example.com/"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com./a", "https://example.com/a"),
        ("http://[::1]:8000/x", "http://[::1]:8000/x"),
        ("  https://пример.рф/страница ".strip(), "https://xn--e1afmkfd.xn--p1ai/страница"),
    ],
)
def test_normalize_gives_one_form(raw, expected):
    assert ux.normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "x.com", "file:///etc/passwd", "ftp://x.com", "https://", "https:///a", "https://u:p@x.com/",
     "https://x.com\\@evil.example/", "https://x.com/a b", "https://x.com:99999/", "javascript:alert(1)"],
)  # fmt: skip
def test_normalize_refuses_what_a_browser_could_read_differently(raw):
    with pytest.raises(ValueError):
        ux.normalize(raw)


# --- ответ перехода и сверка домена ---------------------------------------------------------------------


def test_final_address_is_read_from_the_page_block_of_the_real_response_format():
    assert ux.final_url(nav_response(PAGE)) == PAGE
    assert ux.final_url(NAV_RESPONSE.format(url=PAGE, final="http://localhost:8123/cards.html")) == (
        "http://localhost:8123/cards.html"
    )
    assert ux.final_url([{"type": "text", "text": "- Page URL: https://a.example/x\r\n- Page Title: t"}]) == (
        "https://a.example/x"
    )
    assert ux.final_url("### Result\nready") is None and ux.final_url(None) is None


def test_a_page_title_cannot_forge_the_final_address():
    text = "### Page\n- Page URL: https://real.example/\n- Page Title: x - Page URL: https://evil.example/"
    assert ux.final_url(text) == "https://real.example/"


@pytest.mark.parametrize(
    "requested, final, status",
    [
        ("https://x.com/app", "https://x.com/app/step-2", ux.CONFIRMED),
        ("https://x.com/app", "https://www.x.com/app", ux.CONFIRMED),  # тот же сайт с `www.`
        ("https://www.x.com/app", "https://X.com./", ux.CONFIRMED),
        ("http://x.com/app", "https://x.com/app", ux.CONFIRMED),  # http -> https на том же домене
        ("https://x.com/app", "https://evil.example/", ux.FOREIGN),
        ("https://x.com/app", "https://x.com.evil.example/", ux.FOREIGN),  # не «начинается с»
        ("https://x.com/app", "https://evilx.com/", ux.FOREIGN),
        ("http://127.0.0.1:8123/go", "http://localhost:8123/cards.html", ux.FOREIGN),  # как в пробе
        ("https://x.com/app", "about:blank", ux.FOREIGN),
        ("https://x.com/app", "chrome-error://chromewebdata/", ux.FOREIGN),
    ],
)
def test_verify_compares_the_domain_of_the_final_address(requested, final, status):
    assert ux.verify(requested, nav_response(requested, final)).status == status


NO_PAGE = {
    "content": [{"type": "text", "text": "### Ran Playwright code\n```js\nawait page.goto('u');\n```"}]
}


def test_a_response_without_the_final_address_never_confirms_by_itself():
    """Сервер называет адрес, только если изменился заголовок вкладки (проверено пробой и кодом пакета)."""
    assert ux.verify("https://x.com/app", NO_PAGE).status == ux.PENDING  # прежнего итога нет — закрыто
    assert ux.verify("https://x.com/app", NO_PAGE, "https://x.com/app").status == ux.CONFIRMED
    assert ux.verify("https://x.com/app", NO_PAGE, "https://evil.example/").status == ux.FOREIGN
    # ответ с «Page URL» главнее прежнего итога
    assert ux.verify(
        "https://x.com/app", nav_response("https://x.com/app"), "https://evil.example/"
    ).status == (ux.CONFIRMED)
    limits = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
    assert "последний известный итоговый адрес" in limits and "снимок закрыт" in limits


def test_a_shot_response_with_another_domain_makes_the_shot_unfit():
    prior = ux.NavState(ux.CONFIRMED, "https://x.com/app", "https://x.com/app")
    assert (
        ux.verify_shot(prior, nav_response("https://x.com/app", "https://evil.example/")).status == ux.FOREIGN
    )
    assert ux.verify_shot(prior, nav_response("https://x.com/app", "https://x.com/b")).status == ux.CONFIRMED
    assert (
        ux.verify_shot(prior, NO_PAGE) is None
        and ux.verify_shot(None, nav_response("https://x.com/")) is None
    )


def test_unicode_and_punycode_hosts_are_one_address_and_one_site():
    assert ux.normalize("https://ПРИМЕР.рф/a") == ux.normalize("https://xn--e1afmkfd.xn--p1ai/a")
    assert ux.normalize("https://пример.рф/") == "https://xn--e1afmkfd.xn--p1ai/"
    assert ux.same_site("https://пример.рф/", "https://xn--e1afmkfd.xn--p1ai/path")
    assert ux.verify(
        "https://пример.рф/", nav_response("https://пример.рф/", "https://xn--e1afmkfd.xn--p1ai/")
    ).status == (ux.CONFIRMED)
    assert not ux.same_site("https://пример.рф/", "https://xn--e1aybc.xn--p1ai/")
    with pytest.raises(ValueError):
        ux.normalize("https://" + "я" * 70 + ".рф/")  # метка длиннее 63 знаков не кодируется
    with pytest.raises(ValueError):
        ux.normalize("https://a..b/")


# --- команды ux -------------------------------------------------------------------------------------------


def test_ux_add_writes_the_list_and_ux_list_reads_it(demo, cli):
    code, out, _ = cli("ux", "add", "HTTPS://Example.com/App#x")
    assert code == 0 and "https://example.com/App" in out
    data = yaml.safe_load((demo / "ux-targets.yaml").read_text(encoding="utf-8"))
    assert [t["url"] for t in data["targets"]] == ["https://example.com/App"]
    assert cli("ux", "add", "https://example.com/App")[0] == 0  # повтор не дублирует
    assert cli("ux", "add", "https://example.com/other")[0] == 0
    code, out, _ = cli("ux", "list")
    assert code == 0 and out.splitlines() == ["https://example.com/App", "https://example.com/other"]
    code, out, _ = cli("ux", "list", "--json")
    assert json.loads(out)["targets"] == ["https://example.com/App", "https://example.com/other"]
    kinds = [json.loads(x) for x in (demo / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["name"] for e in kinds if e.get("name") == "strateg ux add"] == ["strateg ux add"] * 2
    assert cli("check")[0] == 0  # список — не ошибка проверки проекта


def test_ux_list_of_a_fresh_project_says_how_to_add(demo, cli):
    code, out, _ = cli("ux", "list")
    assert code == 0 and "strateg ux add" in out


@pytest.mark.parametrize("sub", [("add", "https://example.com"), ("list",), ("import", "x.png")])
def test_ux_without_an_active_project_is_a_clear_error(home, clock, cli, sub):
    code, out, err = cli("ux", *sub)
    assert code == 1 and "активный проект не выбран" in err and "strateg use" in err


@pytest.mark.parametrize("bad", ["example.com", "file:///x", "https://u:p@example.com", "https://x.com/a b"])
def test_ux_add_refuses_a_bad_address_without_writing(demo, cli, bad):
    code, _, err = cli("ux", "add", bad)
    assert code == 2 and "не принят" in err
    assert not (demo / "ux-targets.yaml").exists()


def test_ux_list_refuses_a_broken_file_and_the_hook_reads_it_as_empty(demo, cli):
    (demo / "ux-targets.yaml").write_text("targets: [oops", encoding="utf-8")
    code, _, err = cli("ux", "list")
    assert code == 1 and "не читается" in err
    assert ux.read_urls(demo) == ()  # хук: ошибка чтения — переход закрыт


# --- снимок: из временной папки сервера в inputs/ ---------------------------------------------------------


@pytest.fixture
def shots(tmp_path, monkeypatch) -> Path:
    temp = tmp_path / "temp"
    (temp / ux.SHOTS_NAME).mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))  # `tempfile.gettempdir()` — папка снимков сервера
    return temp / ux.SHOTS_NAME


def test_ux_import_puts_the_screenshot_into_inputs_and_add_takes_it_from_there(demo, cli, shots):
    shot = shots / "page-2026-11-20T18-21-15-126Z.png"
    shot.write_bytes(png_bytes(4))
    code, out, _ = cli("ux", "import", str(shot))
    assert code == 0 and "inputs/ux-page-2026-11-20T18-21-15-126Z.png" in out and "add --confirm" in out
    assert (demo / "inputs" / "ux-page-2026-11-20T18-21-15-126Z.png").read_bytes() == shot.read_bytes()
    code, _, err = cli("ux", "import", str(shot))
    assert code == 0, err  # повтор с тем же содержимым — тот же файл
    code, out, _ = cli("add", "inputs/ux-page-2026-11-20T18-21-15-126Z.png")
    assert code == 0 and "изображение" in out


def test_ux_import_takes_only_pictures_from_the_servers_folder(demo, cli, shots, tmp_path):
    stray = tmp_path / "page.png"
    stray.write_bytes(png_bytes(4))
    assert "должен лежать" in cli("ux", "import", str(stray))[2]
    secret = shots / "notes.txt"
    secret.write_text("x", encoding="utf-8")
    assert "нужен файл снимка" in cli("ux", "import", str(secret))[2]
    assert "должен лежать" in cli("ux", "import", str(shots / ".." / ".." / "page.png"))[2]
    assert "нужен файл снимка" in cli("ux", "import", str(shots / "missing.png"))[2]
    assert not (demo / "inputs").exists() or not list((demo / "inputs").glob("ux-*"))


# --- хуки: переход по списку и сверка домена (скрипты как в Claude Code) ------------------------------------


def role_event(event_name: str, tool: str, tin: dict, product: Path, **extra) -> dict:
    ev = pre_event(tool, tin, cwd=str(product), agent_type="researcher", agent_id="a1", **extra)
    ev["hook_event_name"] = event_name
    return ev


def pre(product, home, tool, tin, agent="researcher"):
    ev = pre_event(tool, tin, cwd=str(product), **({"agent_type": agent, "agent_id": "a1"} if agent else {}))
    proc = run_hook(product, "pre_tool_use.py", ev, STRATEG_PROJECTS_DIR=str(home))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    text = proc.stdout.decode("utf-8").strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def post(product, home, tin, response, agent="researcher"):
    ev = role_event("PostToolUse", NAV, tin, product, tool_response=response)
    if agent != "researcher":
        ev["agent_type"] = agent
    proc = run_hook(product, "post_tool_use.py", ev, STRATEG_PROJECTS_DIR=str(home))
    assert proc.returncode == 0 and proc.stderr == b"", proc.stderr.decode("utf-8", "replace")
    text = proc.stdout.decode("utf-8").strip()
    return json.loads(text) if text else None


def denied(out, *words):
    assert out is not None and out["permissionDecision"] == "deny", out
    for w in words:
        assert w in out["permissionDecisionReason"], out["permissionDecisionReason"]


@pytest.fixture
def listed(demo, cli, home, product):
    assert cli("ux", "add", PAGE)[0] == 0
    return home


def test_navigation_only_by_the_list_and_screenshot_only_after_a_confirmed_navigation(listed, demo, product):
    home = listed
    denied(pre(product, home, NAV, {"url": "http://127.0.0.1:8123/other"}), "нет в списке", "strateg ux add")
    denied(pre(product, home, SHOT, {}), "сначала `browser_navigate`")  # перехода ещё не было
    assert pre(product, home, NAV, {"url": PAGE}) is None  # по списку — да
    denied(pre(product, home, SHOT, {}), "не подтверждён")  # переход пошёл, итога нет: закрыто
    assert post(product, home, {"url": PAGE}, nav_response(PAGE)) is None
    assert pre(product, home, SHOT, {}) is None  # домен тот же: снимок разрешён


def test_a_redirect_to_a_foreign_domain_closes_the_screenshot_until_a_new_listed_navigation(
    listed, demo, product
):
    home = listed
    assert pre(product, home, NAV, {"url": PAGE}) is None
    out = post(product, home, {"url": PAGE}, nav_response(PAGE, "http://evil.example/login"))
    assert out["decision"] == "block" and "evil.example" in out["reason"]
    denied(pre(product, home, SHOT, {}), "другой домен", "evil.example")
    denied(pre(product, home, SHOT, {"fullPage": True}), "другой домен")  # любые параметры
    events = [json.loads(x) for x in (demo / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "отказ хука" and "evil.example" in e.get("message", "") for e in events)
    # новый переход по списку, на этот раз без ухода: снимок снова разрешён
    assert pre(product, home, NAV, {"url": PAGE}) is None
    denied(pre(product, home, SHOT, {}), "не подтверждён")  # прежний итог сброшен, пока не сверен новый
    assert post(product, home, {"url": PAGE}, nav_response(PAGE)) is None
    assert pre(product, home, SHOT, {}) is None


def test_a_failed_post_hook_leaves_the_screenshot_closed(listed, product):
    home = listed
    assert pre(product, home, NAV, {"url": PAGE}) is None
    # PostToolUse не сработал (сбой, выключен, ответ не дошёл): итог остался «ждёт сверки»
    denied(pre(product, home, SHOT, {}), "не подтверждён")


def test_a_first_response_without_the_final_address_leaves_the_screenshot_closed(listed, product):
    home = listed
    assert pre(product, home, NAV, {"url": PAGE}) is None
    assert post(product, home, {"url": PAGE}, NO_PAGE) is None
    denied(pre(product, home, SHOT, {}), "не подтверждён")  # итога прежде не было — подтверждать нечем


def test_a_repeated_navigation_without_page_block_keeps_the_last_known_final_address(listed, product):
    """Ответ пробы на повторный переход: блока «Page» нет, а вкладка осталась на прежнем (чужом) адресе."""
    home = listed
    assert pre(product, home, NAV, {"url": PAGE}) is None
    out = post(product, home, {"url": PAGE}, nav_response(PAGE, "http://localhost:8123/cards.html"))
    assert out["decision"] == "block" and "Не повторяй" in out["reason"]
    assert pre(product, home, NAV, {"url": PAGE}) is None  # исследователь повторил переход
    assert post(product, home, {"url": PAGE}, NO_PAGE)["decision"] == "block"  # итог прежний: чужой
    denied(pre(product, home, SHOT, {}), "другой домен", "localhost")
    # своя страница: повтор без блока «Page» оставляет подтверждение
    assert pre(product, home, NAV, {"url": PAGE}) is None
    assert post(product, home, {"url": PAGE}, nav_response(PAGE)) is None
    assert pre(product, home, NAV, {"url": PAGE}) is None
    assert post(product, home, {"url": PAGE}, NO_PAGE) is None
    assert pre(product, home, SHOT, {}) is None


def test_the_shot_response_updates_the_final_address_and_a_foreign_page_makes_it_unfit(listed, demo, product):
    home = listed
    assert pre(product, home, NAV, {"url": PAGE}) is None
    assert post(product, home, {"url": PAGE}, nav_response(PAGE)) is None
    assert pre(product, home, SHOT, {}) is None
    ev = role_event(
        "PostToolUse", SHOT, {}, product, tool_response=nav_response(PAGE, "http://evil.example/x")
    )
    proc = run_hook(product, "post_tool_use.py", ev, STRATEG_PROJECTS_DIR=str(home))
    out = json.loads(proc.stdout.decode("utf-8"))
    assert out["decision"] == "block" and "снимок не годится" in out["reason"]
    denied(pre(product, home, SHOT, {}), "другой домен", "evil.example")
    events = [json.loads(x) for x in (demo / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("name") == "browser_take_screenshot" and e["kind"] == "отказ хука" for e in events)


def test_the_hook_cannot_be_walked_around_by_the_roles_that_have_no_browser(listed, product):
    home = listed
    for agent in (None, "critic"):
        denied(pre(product, home, NAV, {"url": PAGE}, agent), "Браузер закрыт")
        denied(pre(product, home, SHOT, {}, agent), "Браузер закрыт")
    denied(pre(product, home, "mcp__playwright__browser_snapshot", {}), "только `browser_navigate`")
    denied(pre(product, home, "mcp__playwright__browser_evaluate", {"function": "()=>1"}), "только")
    # ответ чужой роли итога не пишет
    assert post(product, home, {"url": PAGE}, nav_response(PAGE, "http://evil.example/"), "critic") is None
    assert not (listed / "demo" / ".tmp" / "ux-nav.json").exists()


def test_state_file_of_the_navigation_cannot_be_forged_by_the_main_agent(listed, product):
    home = listed
    state = str(home / "demo" / ".tmp" / "ux-nav.json")
    out = pre(product, home, "Write", {"file_path": state, "content": "{}"}, agent=None)
    denied(out, "CLI")
    out = pre(product, home, "Bash", {"command": f"echo {{}} > {state}"}, agent=None)
    denied(out, "CLI")


# --- команда `ux add` у человека, у агентов и в среде ---------------------------------------------------


def test_ux_add_is_asked_of_the_human_and_never_passes_silently(listed, product):
    home = listed
    assert "Bash(uv run strateg ux add*)" in SETTINGS["permissions"]["ask"]
    assert not SETTINGS["permissions"].get("allow")  # ничто не снимает вопрос
    command = {"command": "uv run strateg ux add https://example.com/new"}
    assert pre(product, home, "Bash", command, agent=None) is None  # хук молчит: решает правило `ask`
    denied(pre(product, home, "Bash", command), "разрешена одна команда Bash")  # исследователю — отказ
    denied(pre(product, home, "Bash", command, "critic"), "закрыто всё")
    # а правило ask сидит на команде, которой пишет список только CLI
    denied(
        pre(
            product,
            home,
            "Write",
            {"file_path": str(home / "demo" / "ux-targets.yaml"), "content": "x"},
            None,
        ),
        "CLI",
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run --frozen strateg ux add https://variant.example/",
        "strateg ux add https://variant.example/",
        "uv run strateg  ux add https://variant.example/",
        "true; uv run strateg ux add https://variant.example/",
        ".venv/Scripts/strateg.exe ux add https://variant.example/",
        "uv run strateg --project demo ux add https://variant.example/",
        "uv run strateg ux --json add https://variant.example/",
        "uv run strateg ux --project demo add https://variant2.example/",
        "uv run strateg ux add https://variant.example/ && echo",
        "uv run strateg ux add $(whoami)",
        "uv run strateg ux add `id`",
        "uv run strateg ux add https://a.example/ | cat",
        "uv run strateg ux add https://a.example/ https://b.example/",
        "uv run strateg ux add",
        "python -m strateg ux add https://variant.example/",
        "uv run strateg UX ADD https://variant.example/",
    ],
)
def test_main_agent_may_only_run_the_exact_ux_add_command(listed, product, command):
    denied(pre(product, listed, "Bash", {"command": command}, agent=None), "ровно `uv run strateg ux add")


@pytest.mark.parametrize(
    "command",
    [
        "uv run strateg ux add https://variant.example/",
        "uv run strateg ux add 'https://variant.example/a?x=1&y=2'",
        'uv run strateg ux add "https://variant.example/"',
        "uv run strateg ux list",
        "uv run strateg add --confirm И-1",
    ],
)
def test_the_exact_ux_add_command_and_other_commands_are_left_to_the_ask_rule(listed, product, command):
    assert pre(product, listed, "Bash", {"command": command}, agent=None) is None


def test_main_agent_may_run_ux_import_from_the_shots_folder(listed, product, shots):
    home = listed
    command = {"command": f"uv run strateg ux import {shots / 'page-1.png'}"}
    assert pre(product, home, "Bash", command, agent=None) is None


def test_post_tool_use_matcher_covers_navigation_and_screenshot_only():
    matcher = SETTINGS["hooks"]["PostToolUse"][0]["matcher"]
    assert re.search(matcher, NAV) and re.search(matcher, SHOT)
    assert re.search(matcher, "mcp__plugin_playwright_playwright__browser_navigate")
    assert not re.search(matcher, "mcp__playwright__browser_navigate_back")
    assert not re.search(matcher, "mcp__playwright__browser_snapshot")


def test_post_hook_script_never_prints_on_garbage_and_never_blocks(product):
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(product / ".claude" / "hooks" / "post_tool_use.py")],
        input=b"\xff\xfe garbage",
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(product / "src")},
    )
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


# --- роли и инструменты -------------------------------------------------------------------------------------


def tools_of(name: str) -> list[str]:
    text = (ROOT / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
    meta = yaml.safe_load(re.match(r"---\r?\n(.*?)\r?\n---", text, re.DOTALL).group(1))
    raw = meta["tools"]
    return raw if isinstance(raw, list) else [t.strip() for t in str(raw).split(",") if t.strip()]


def test_researcher_has_exactly_two_playwright_tools_and_no_one_else_has_any():
    assert [t for t in tools_of("researcher") if "playwright" in t or "browser_" in t] == [
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_take_screenshot",
    ]
    for wrapper in sorted((ROOT / ".claude" / "agents").glob("*.md")):
        if wrapper.stem != "researcher":
            assert not [t for t in tools_of(wrapper.stem) if t.startswith("mcp__")], wrapper.name
    assert {"WebSearch", "Read", "Grep", "Glob", "Bash"} <= set(tools_of("researcher"))
    allowed = {t.removeprefix("mcp__playwright__") for t in tools_of("researcher") if t.startswith("mcp__")}
    assert allowed == rules.PLAYWRIGHT_ALLOWED  # обёртка и хук называют одни и те же инструменты


def test_main_agent_and_critic_never_get_the_browser_even_when_the_list_has_the_address():
    svc = services()
    for agent in (None, "critic"):
        for tool in (NAV, SHOT, "mcp__playwright__browser_click"):
            assert decide(tool, {"url": "https://x.com/"}, agent, svc=svc).denied


def test_researcher_role_text_says_when_to_shoot_what_to_return_and_what_not_to_do():
    role = (ROOT / "knowledge" / "roles" / "researcher.md").read_text(encoding="utf-8")
    section = role.split("## Снимок экрана", 1)[1].split("\n## ", 1)[0]
    for phrase in ("списке", "адрес", "путь файла", "strateg fetch", "без выводов о людях"):
        assert phrase in section, phrase


# --- .mcp.json ---------------------------------------------------------------------------------------------


LAUNCHER = ROOT / "scripts" / "playwright_mcp.py"


def launcher():
    import importlib.util

    spec = importlib.util.spec_from_file_location("playwright_mcp", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server_args(platform="win32", env=None, temp="/t") -> list[str]:
    return launcher().build_command("npx", platform, env or {}, temp)


def test_mcp_server_is_pinned_and_limited():
    server = MCP["mcpServers"]["playwright"]
    assert list(MCP["mcpServers"]) == ["playwright"] and server["command"] == "uv"
    assert server["args"] == ["run", "--frozen", "python", "-X", "utf8", "scripts/playwright_mcp.py"]
    assert "${" not in json.dumps(MCP) and "cmd" not in json.dumps(MCP)  # без обёртки и без переменных
    assert LAUNCHER.is_file()
    args = server_args()
    package = [a for a in args if a.startswith("@playwright/mcp")]
    assert len(package) == 1 and re.fullmatch(r"@playwright/mcp@\d+\.\d+\.\d+", package[0])
    assert "latest" not in json.dumps(MCP) and "-y" in args
    assert package[0] == "@playwright/mcp@0.0.84"
    for flag in ("--headless", "--isolated"):
        assert flag in args
    assert args[args.index("--snapshot-mode") + 1] == "none"  # текст страницы не идёт в ответы на действия


def test_mcp_server_has_no_filename_and_writes_outside_the_project():
    text = (ROOT / ".mcp.json").read_text(encoding="utf-8") + LAUNCHER.read_text(encoding="utf-8")
    assert "filename" not in text.lower()
    args = server_args(temp=str(Path(tempfile.gettempdir())))
    out = args[args.index("--output-dir") + 1]
    assert Path(out) == Path(tempfile.gettempdir()) / ux.SHOTS_NAME  # временная папка ОС, не папка проекта
    assert ROOT not in Path(out).parents
    assert args[args.index("--file-paths") + 1] == "absolute"  # путь снимка — готовый для `ux import`
    assert not re.search(r"[A-Za-z]:[\\/]|/Users/|/home/", text)  # личных путей нет
    assert not re.search(r"(?i)key|token|secret|password", text)


def test_mcp_browser_follows_the_os_and_the_environment_may_override_it():
    def browser(platform, env=None):
        args = server_args(platform, env)
        return args[args.index("--browser") + 1]

    assert browser("win32") == "msedge" and browser("darwin") == "chrome" and browser("linux") == "chrome"
    assert browser("darwin", {"STRATEG_BROWSER": "msedge"}) == "msedge"
    assert browser("win32", {"STRATEG_BROWSER": " "}) == "msedge"
    docs = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
    assert "STRATEG_BROWSER" in docs and "chrome" in docs.lower() and "cmd /c" not in docs


def test_the_shots_folder_of_the_server_and_of_the_cli_are_the_same():
    assert launcher().SHOTS_NAME == ux.SHOTS_NAME
    assert ux.shots_dir() == Path(tempfile.gettempdir()) / ux.SHOTS_NAME
    out = server_args(temp=str(Path(tempfile.gettempdir())))
    assert (
        Path(out[out.index("--output-dir") + 1]) == ux.shots_dir()
    )  # сервер пишет туда, откуда берёт `ux import`


def test_launcher_finds_npx_and_reports_when_it_is_missing(monkeypatch, capsys):
    mod = launcher()
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod.main() == 1 and "Node" in capsys.readouterr().err


def test_launcher_passes_stdio_through_on_windows_and_replaces_the_process_elsewhere(monkeypatch):
    mod = launcher()
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/npx")
    calls = []
    monkeypatch.setattr(mod.subprocess, "call", lambda argv: calls.append(("call", argv)) or 0)
    monkeypatch.setattr(mod.os, "execv", lambda exe, argv: calls.append(("execv", exe)))
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert mod.main() == 0 and calls[0][0] == "call" and calls[0][1][:3] == ["/bin/npx", "-y", mod.PACKAGE]
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    mod.main()
    assert calls[1] == ("execv", "/bin/npx")


def test_the_probe_starts_the_server_with_exactly_the_command_of_mcp_json():
    text = (ROOT / "scripts" / "probe_playwright_mcp.py").read_text(encoding="utf-8")
    assert 'entry["command"]' in text and 'entry.get("args", [])' in text and "argv[argv.index" not in text


# --- doctor ------------------------------------------------------------------------------------------------


def test_doctor_without_node_is_a_warning_not_an_error(capsys, monkeypatch):
    warn = ("WARN", "не найден: снимки экрана интерфейсов недоступны, разбор — по снимкам человека")
    code = cli_module.cmd_doctor(
        type("A", (), {"node": warn, "console": ("OK", "x"), "sqlite": ("OK", "x")})()
    )
    lines = capsys.readouterr().out.splitlines()
    node = next(x for x in lines if "Node" in x)
    assert node.startswith("[ПРЕД]") and "снимки экрана интерфейсов недоступны" in node
    assert "разбор — по снимкам человека" in node
    assert code in (0, 1)  # код определяют другие проверки среды, Node его не поднимает
    assert "[НЕТ ] Node" not in "\n".join(lines)


def test_node_line_levels():
    def which(found):
        return lambda name: f"/bin/{name}" if name in found else None

    class Done:
        def __init__(self, out):
            self.stdout = out

    assert ux.node_line(which(()), None)[0] == "WARN"
    assert "недоступны" in ux.node_line(which(()), None)[1]
    assert ux.node_line(which(("node", "npx")), lambda *a, **k: Done("v24.21.0\n")) == ("OK", "v24.21.0")
    level, text = ux.node_line(which(("node", "npx")), lambda *a, **k: Done("v20.11.0\n"))
    assert level == "WARN" and "ниже 22" in text
    assert ux.node_line(which(("node", "npx")), lambda *a, **k: Done("????"))[0] == "WARN"
    assert ux.node_line(which(("node",)), lambda *a, **k: Done("v24.0.0"))[0] == "WARN"  # нет npx

    def broken(*a, **k):
        raise OSError("нет доступа")

    assert ux.node_line(which(("node", "npx")), broken)[0] == "WARN"


# --- проба сервера: только по флагу, браузер и сеть в обычный pytest не входят -----------------------------


@pytest.mark.skipif(
    os.environ.get("STRATEG_PROBE_PLAYWRIGHT") != "1" or shutil.which("npx") is None,
    reason="проба идёт по флагу STRATEG_PROBE_PLAYWRIGHT=1, нужен Node (scripts/probe_playwright_mcp.py)",
)
def test_live_probe_of_the_server(tmp_path):
    import subprocess

    out = tmp_path / "shots"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "probe_playwright_mcp.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "browser_take_screenshot" in report["tools"]
    assert any(f.endswith(".png") for f in report["files"])
    assert "- Page URL:" in report["navigate"][0]


# --- импорт: другой файл не затирается; путь к функции виден основному агенту ------


def test_ux_import_does_not_overwrite_a_different_file_with_the_same_name(demo, cli, shots):
    shot = shots / "page-1.png"
    shot.write_bytes(png_bytes(4))
    assert cli("ux", "import", str(shot))[0] == 0
    shot.write_bytes(png_bytes(9))
    code, _, err = cli("ux", "import", str(shot))
    assert code == 1 and "другим содержимым" in err
    assert (demo / "inputs" / "ux-page-1.png").read_bytes() == png_bytes(4)


def test_the_way_to_a_screenshot_is_visible_to_the_main_agent():
    skill = (ROOT / ".claude" / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
    section = skill.split("### Изображения и снимки экрана", 1)[1].split("\n## ", 1)[0]
    assert "«Снимок экрана: <адрес>»" in section and "ux list" in section and "ux add" in section
    assert len(section.encode("utf-8")) <= 1500
    assert "снимок экрана по адресу из списка разбора" in (
        ROOT / ".claude" / "agents" / "researcher.md"
    ).read_text(encoding="utf-8")
    role = (ROOT / "knowledge" / "roles" / "researcher.md").read_text(encoding="utf-8")
    assert "не повторяй" in role and "итоговый домен" in role


def test_documents_say_the_server_is_started_by_the_launcher_and_not_run_from_claude_code():
    limits = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "adr" / "0013-mcp.md").read_text(encoding="utf-8")
    for text in (limits, adr):
        assert "корень проекта" in text and "не проверен" in text
    assert "один исследователь за раз" in limits and "временная папка" in limits
