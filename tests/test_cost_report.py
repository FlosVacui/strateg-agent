"""`strateg cost report`: стоимость по ролям, шагам и моделям из `cost.jsonl` (ADR-0010)."""

from __future__ import annotations

import json

import pytest

from strateg import accounting
from strateg.hooks import effects
from strateg.hooks.rules import EventSpec
from transcripts import HAIKU, OPUS, SONNET, agent_file, assistant, session_files, write


@pytest.fixture(autouse=True)
def hook_clock(monkeypatch, clock):
    monkeypatch.setattr(effects, "now", clock)


@pytest.fixture
def demo(cli, home):
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def src(key, path, role):
    return accounting.Source(key, path, role)


@pytest.fixture
def busy(cli, demo, tmp_path):
    """Две сессии и три роли: основной агент и исследователь в первой, основной агент и критик во второй."""
    main1, folder1 = session_files(tmp_path / "c", "s1")
    main2, folder2 = session_files(tmp_path / "c", "s2")
    write(main1, assistant("a1", SONNET, out=60_000), assistant("a2", SONNET, out=10_000))  # $0,70
    write(
        folder1 / "agent-r1.jsonl",
        assistant("r1", HAIKU, inp=1000, out=100_000, web=2),
    )
    accounting.account(
        demo,
        "s1",
        [src("main", main1, "основной"), src("agent:r1", folder1 / "agent-r1.jsonl", "researcher")],
    )
    assert cli("step", "start", "2а")[0] == 0
    write(main2, assistant("b1", OPUS, out=10_000))  # $0,20
    agent_file(folder2, "c1", assistant("c1", "claude-future-9", out=500))
    accounting.account(
        demo, "s2", [src("main", main2, "основной"), src("agent:c1", folder2 / "agent-c1.jsonl", "critic")]
    )
    return demo


def report(cli, *extra):
    code, out, err = cli("cost", "report", *extra)
    assert code == 0, err
    return json.loads(out) if "--json" in extra else out


def by(rows, key):
    return {r[key]: r for r in rows}


def test_report_sums_roles_steps_and_models_from_the_cost_file(cli, busy):
    d = report(cli, "--json")
    haiku = 1000 * 0.1 / 1e6 + 100_000 * 0.5 / 1e6 + 2 * 0.01  # токены + два веб-поиска
    total = 70_000 * 10 / 1e6 + haiku + 10_000 * 20 / 1e6
    assert d["ok"] is True and d["project"] == "demo"
    assert d["total_usd"] == pytest.approx(total, abs=1e-6)
    assert d["precision"] == "оценка" and d["complete"] is False  # у критика модель без цены
    assert d["sessions"] == 2 and d["records"] == 5

    roles = by(d["roles"], "role")
    assert set(roles) == {"основной", "researcher", "critic"}
    assert roles["основной"]["calls"] == 3 and roles["основной"]["usd"] == pytest.approx(0.9, abs=1e-6)
    assert roles["основной"]["models"] == [OPUS, SONNET]
    assert roles["основной"]["output_tokens"] == 80_000
    assert roles["researcher"]["web_searches"] == 2 and roles["researcher"]["models"] == [HAIKU]
    assert roles["critic"]["usd"] is None and roles["critic"]["unpriced_calls"] == 1
    assert roles["critic"]["output_tokens"] == 500  # токены записаны, хотя цены нет
    assert sum(r["share"] or 0 for r in d["roles"]) == pytest.approx(1.0, abs=1e-3)
    assert roles["основной"]["share"] == pytest.approx(0.9 / total, abs=1e-3)

    steps = by(d["steps"], "step")
    assert set(steps) == {"1", "2а"} and steps["1"]["calls"] == 3 and steps["2а"]["calls"] == 2
    assert steps["1"]["usd"] == pytest.approx(0.7 + haiku, abs=1e-6)
    assert by(d["models"], "model")["claude-future-9"]["unpriced_calls"] == 1


def test_report_names_unpriced_models_skipped_records_and_the_price_date(cli, busy):
    d = report(cli, "--json")
    assert d["unpriced_models"] == [{"model": "claude-future-9", "calls": 1}]
    assert d["skipped_records"] == 0 and d["prices_accessed"] == "2026-11-08"


def test_report_counts_skipped_transcript_records(cli, busy, tmp_path):
    main, _ = session_files(tmp_path / "c", "s3")
    write(main, "битая строка", '{"type": "assistant", "message": {"id": "q"}}', assistant("z1", out=1))
    accounting.account(busy, "s3", [src("main", main, "основной")])
    assert report(cli, "--json")["skipped_records"] == 2


def test_report_text_has_total_precision_roles_steps_and_the_subscription_note(cli, busy):
    text = report(cli)
    assert "Стоимость проекта demo: $" in text and "(оценка)" in text and "часть записей без цены" in text
    assert "По ролям:" in text and "По шагам:" in text and "По моделям:" in text
    assert "researcher:" in text and "critic:" in text
    assert "веб-поиск 2" in text
    assert "Модели без цены в config/prices.yaml: claude-future-9 (1)" in text
    assert "эквивалент по цене API" in text and "оценка" in text
    assert "обращение к странице цен 2026-11-08" in text


def test_report_lists_model_upgrades_from_the_journal(cli, busy):
    effects.append_event(
        busy,
        EventSpec(
            "выбор модели",
            "повышение: средняя -> сильная, условие: итог не пройден",
            name="повышение",
            role="critic",
            model="opus",
            level="сильная",
            effort="high",
        ),
    )
    d = report(cli, "--json")
    (up,) = d["model_upgrades"]
    assert (up["role"], up["model"], up["step"]) == ("critic", "opus", "2а")
    assert "шаг 2а: critic -> opus" in report(cli)


def test_report_for_an_empty_project_says_there_is_no_data(cli, demo):
    d = report(cli, "--json")
    assert d["total_usd"] is None and d["precision"] == "нет данных" and d["roles"] == []
    assert "нет данных" in report(cli)


def test_report_takes_the_measured_total_of_a_session_over_its_messages(cli, demo):
    """Стенд: итог сессии от среды (измерено) и роль в разбивке; точность называется."""
    assert cli("cost", "add", "--session", "stand", "--role", "стенд", "--model", "m", "--usd", "1.5",
               "--source", "измерено", "--total")[0] == 0  # fmt: skip
    d = report(cli, "--json")
    assert d["total_usd"] == 1.5 and d["precision"] == "измерено" and d["complete"] is True
    assert [r["role"] for r in d["roles"]] == ["стенд"]


def test_report_is_read_only(cli, busy):
    before = {p.name: p.read_bytes() for p in busy.iterdir() if p.is_file()}
    report(cli)
    report(cli, "--json")
    assert {p.name: p.read_bytes() for p in busy.iterdir() if p.is_file()} == before


def test_report_without_a_project_is_an_error(cli, home):
    code, _, err = cli("cost", "report")
    assert code != 0 and err


def test_report_breaks_down_by_messages_when_the_session_total_is_measured(cli, demo, tmp_path):
    """Стенд: итог сессии от среды (`total_cost_usd`) и сообщения хуков: итог измерен, роли — оценка."""
    main, _ = session_files(tmp_path / "c", "s1")
    write(main, assistant("a1", SONNET, out=10_000))  # $0,10
    accounting.account(demo, "s1", [src("main", main, "основной")])
    code, _, err = cli(
        "cost", "add", "--session", "s1", "--role", "стенд", "--model", "m",
        "--usd", "0.5", "--source", "измерено", "--total",
    )  # fmt: skip
    assert code == 0, err
    d = report(cli, "--json")
    assert d["total_usd"] == 0.5 and d["precision"] == "измерено"
    assert [r["role"] for r in d["roles"]] == ["основной"]
    assert d["breakdown_usd"] == pytest.approx(0.1)
    text = report(cli)
    assert "Разбивка ниже — оценка по транскриптам ($0,1000), итог проекта — измерено ($0,5000)." in text


def test_model_without_a_price_is_not_shown_as_zero_dollars(cli, busy):
    """Сумма группы без цены — «нет цены», а не «$0,0000, 0 %»."""
    d = report(cli, "--json")
    future = by(d["models"], "model")["claude-future-9"]
    assert future["usd"] is None and future["share"] is None and future["unpriced_calls"] == 1
    assert by(d["roles"], "role")["critic"]["usd"] is None
    assert by(d["models"], "model")[SONNET]["usd"] is not None
    text = report(cli)
    assert "  claude-future-9: нет цены, —, вызовов 1" in text
    assert "  critic: нет цены, —, вызовов 1" in text
    assert "$0,0000" not in text


def test_partly_priced_group_says_how_many_calls_have_no_price(cli, busy, tmp_path):
    main, _ = session_files(tmp_path / "c", "s4")
    write(main, assistant("p1", SONNET, out=1000), assistant("p2", "claude-future-9", out=5))
    accounting.account(busy, "s4", [src("main", main, "основной")])
    text = report(cli)
    assert "(вызовов без цены: 1)" in text


def test_synthetic_messages_do_not_show_up_as_skipped_records(cli, busy, tmp_path):
    main, _ = session_files(tmp_path / "c", "s5")
    fake = json.dumps({"type": "assistant", "message": {"id": "e", "model": "<synthetic>", "usage": {}}})
    write(main, fake, fake, assistant("z1", out=1))
    accounting.account(busy, "s5", [src("main", main, "основной")])
    d = report(cli, "--json")
    assert d["skipped_records"] == 0
    assert "Пропущено при разборе" not in report(cli)
