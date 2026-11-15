"""Выборка и скрипт замера классификатора: доли, ловушки, разбор заготовленных ответов без сети и claude."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import eval_classifier as ev
from strateg import classify

PAIRS = ev.load_pairs()
TRAPS = {
    "сильнее цитаты",
    "не тот период",
    "не тот регион",
    "число без знаменателя",
    "прогноз выдан за факт",
    "инструкция в цитате",
}
SHARES = {"подтверждает": 0.40, "не подтверждает": 0.25, "частично": 0.20, "неверный тип": 0.15}


def test_sample_has_fifty_pairs_with_the_planned_shares():
    assert len(PAIRS) >= 50
    groups = Counter(p["group"] for p in PAIRS)
    for group, share in SHARES.items():
        assert abs(groups[group] / len(PAIRS) - share) <= 0.03, (group, groups[group])


def test_groups_agree_with_the_labels():
    for p in PAIRS:
        type_ok = p["type"] == p["expected"]["type"]
        if p["group"] == "неверный тип":
            assert not type_ok, p["id"]
        else:
            assert type_ok and p["expected"]["verdict"] == p["group"], p["id"]


def test_sample_has_every_trap_and_injections_with_every_verdict():
    traps = Counter(p.get("trap") for p in PAIRS if p.get("trap"))
    assert set(traps) == TRAPS and all(n >= 1 for n in traps.values())
    injected = {p["expected"]["verdict"] for p in PAIRS if p.get("trap") == "инструкция в цитате"}
    assert injected == {"подтверждает", "частично", "не подтверждает"}  # инструкция не диктует ответ


def test_pairs_have_whole_quotes_and_render_like_the_product():
    for p in PAIRS:
        assert all("…" not in q["text"] for q in p["quotes"]), p["id"]
    task = classify.render_task([ev.to_pair(p) for p in PAIRS[:3]], classify.role_text())
    assert task.startswith("# Роль: классификатор")
    assert task.split(classify.PAIRS_HEADING, 1)[1].count("(недоверенный текст)") == 3


def dry(capsys, *extra) -> dict:
    assert ev.main(["--dry-run", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_dry_run_parses_the_canned_answers(capsys):
    d = dry(capsys)
    assert d["dry_run"] is True and d["batch"] == 10
    h, s = d["results"]["haiku"], d["results"]["sonnet"]
    # haiku: строка не по формату (У-15) и две разные строки (У-29) — «не уверен», а не «подтверждает»
    assert h["unparsed"] == ["У-15", "У-29"]
    assert h["verdicts"]["У-15"] == h["verdicts"]["У-29"] == "не уверен"
    assert h["wrong"] == ["У-15", "У-19", "У-22", "У-29", "У-35", "У-38", "У-47"]
    assert h["false_support"] == ["У-22", "У-38"]
    assert h["verdict_accuracy"] == round(45 / 52, 4) and h["type_accuracy"] == round(49 / 52, 4)
    assert h["unsure_share"] == round(3 / 52, 4) and h["unknown_ids"] == ["У-99"]
    assert h["matrix"]["не подтверждает"]["подтверждает"] == 1
    assert sum(sum(row.values()) for row in h["matrix"].values()) == 52
    assert h["cost_usd"] == round(6 * 0.0021, 6) and h["cost_per_pair_usd"] == round(6 * 0.0021 / 52, 8)
    assert s["verdict_accuracy"] == round(51 / 52, 4) and s["type_accuracy"] == 1.0 and s["unparsed"] == []
    assert d["agreement"] == round(44 / 52, 4)
    assert d["decision"] == {"keep": False, "why": "точность 86.5% ниже 90%"}


def test_dry_run_text_report(capsys):
    assert ev.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "haiku: пар 52; вердикт 86.5%" in out and "ложные «подтверждает»: У-22, У-38" in out
    assert "Согласие haiku с sonnet" in out and "Правило ADR-0007 (haiku): убрать" in out


@pytest.mark.parametrize(
    "cheap, ref, keep",
    [
        ({"verdict_accuracy": 0.95, "cost_per_pair_usd": 0.001}, {"cost_per_pair_usd": 0.01}, True),
        ({"verdict_accuracy": 0.89, "cost_per_pair_usd": 0.001}, {"cost_per_pair_usd": 0.01}, False),
        ({"verdict_accuracy": 0.95, "cost_per_pair_usd": 0.01}, {"cost_per_pair_usd": 0.01}, False),
        ({"verdict_accuracy": 0.95, "cost_per_pair_usd": None}, {"cost_per_pair_usd": 0.01}, None),
    ],
)
def test_decision_follows_the_rule_of_the_decision_record(cheap, ref, keep):
    assert ev.decision({"haiku": cheap, "sonnet": ref}, "haiku")["keep"] is keep


def test_claude_call_has_no_tools_and_runs_outside_the_repository(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        body = {"result": "У-1 | подтверждает | тип: да | верно", "total_cost_usd": 0.0012}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body, ensure_ascii=False), "")

    monkeypatch.setattr(ev.subprocess, "run", fake)
    text, cost = ev.ask_claude("haiku", "задание", 0, "low")
    assert text.startswith("У-1 | подтверждает") and cost == 0.0012
    argv = seen["argv"]
    assert argv[:2] == ["claude", "-p"] and argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == "" and "--no-session-persistence" in argv
    assert argv[argv.index("--effort") + 1] == "low" and seen["input"] == "задание"
    assert ev.ROOT not in Path(seen["cwd"]).parents and Path(seen["cwd"]) != ev.ROOT


def test_failed_claude_call_is_unparsed_not_support(monkeypatch):
    monkeypatch.setattr(
        ev.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "нет входа")
    )
    text, cost = ev.ask_claude("haiku", "задание", 0)
    assert cost is None and classify.parse_answer(text, ["У-1"]).items["У-1"].verdict == "не уверен"
