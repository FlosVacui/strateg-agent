"""`strateg cost report`: стоимость по ролям, шагам и моделям из `cost.jsonl` (ADR-0010)."""

from __future__ import annotations

import json
import shutil

import pytest

from strateg import accounting
from strateg.hooks import effects
from strateg.hooks.rules import EventSpec
from transcripts import HAIKU, OPUS, SONNET, agent_file, assistant, searches, session_files, write


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
    assert "веб-поиск: 2 вызова, $0,0200 (оценка)" in text
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


# --- веб-поиск ----------------------------------------------------------------------------------------------

FEE_AND_QUERY = 0.01 + (11840 * 0.1 + 1070 * 0.5) / 1e6


@pytest.fixture
def searching(cli, demo, tmp_path):
    """Исследователь: три поиска и ещё два по данным среды; основной агент: один поиск."""
    return fill_searching(demo, tmp_path)


def fill_searching(demo, tmp_path):
    main, folder = session_files(tmp_path / "w", "s1")
    write(main, assistant("a1", SONNET, out=1000, tools=searches("t0")))
    agent_file(
        folder,
        "r1",
        assistant("r1", SONNET, out=2000, tools=searches("t1", "t2")),
        assistant("r1", SONNET, out=2000, tools=searches("t1", "t2", "t3")),  # тот же ответ, дописан блок
        assistant("r2", SONNET, out=0, web=2),
    )
    accounting.account(
        demo,
        "s1",
        [src("main", main, "основной"), src("agent:r1", folder / "agent-r1.jsonl", "researcher")],
    )
    return demo


def test_report_has_a_web_search_line_in_the_total_and_in_the_roles(cli, searching):
    d = report(cli, "--json")
    # четыре вызова WebSearch — плата и запрос к модели поиска; два поиска из данных среды — одна плата
    calls, usd = 6, 4 * FEE_AND_QUERY + 2 * 0.01
    assert d["web_search"]["calls"] == calls and d["web_search"]["usd"] == pytest.approx(usd, abs=1e-6)
    assert d["web_search"]["precision"] == "оценка"
    roles = by(d["roles"], "role")
    assert roles["researcher"]["web_searches"] == 5
    assert roles["researcher"]["web_search_usd"] == pytest.approx(3 * FEE_AND_QUERY + 2 * 0.01, abs=1e-6)
    assert roles["основной"]["web_searches"] == 1
    assert roles["основной"]["web_search_usd"] == pytest.approx(FEE_AND_QUERY, abs=1e-6)
    # деньги поиска уже в сумме роли, а не сверх неё
    assert roles["основной"]["usd"] == pytest.approx(1000 * 10 / 1e6 + FEE_AND_QUERY, abs=1e-6)


def test_report_text_says_web_search_with_calls_dollars_and_estimate(cli, searching):
    text = report(cli)
    # итоговая строка целиком: подстрока была бы и в строке роли
    assert "веб-поиск: 6 вызовов, $0,0669 (оценка); входит в сумму выше." in text.splitlines()
    researcher = next(line for line in text.splitlines() if line.strip().startswith("researcher:"))
    assert "веб-поиск: 5 вызовов, $0,0552 (оценка)" in researcher


def test_report_without_searches_has_no_web_search_line(cli, demo, tmp_path):
    main, _ = session_files(tmp_path / "n", "s1")
    write(main, assistant("a1", SONNET, out=1000))
    accounting.account(demo, "s1", [src("main", main, "основной")])
    d = report(cli, "--json")
    assert d["web_search"] == {"calls": 0, "usd": 0.0, "precision": "оценка", "query_priced": True}
    assert "веб-поиск" not in report(cli)


def test_records_written_before_the_web_search_field_get_the_fee_by_count(cli, demo):
    """Прежняя запись с `web_searches` и без `web_search_usd` читается: плата за поиск по числу."""
    line = {
        "schema_version": 1,
        "time": "2026-11-06T10:00:00+03:00",
        "session_id": "old",
        "role": "researcher",
        "model": HAIKU,
        "environment": "claude-code",
        "step": "1",
        "entry": "по сообщению",
        "message_id": "old-1",
        "input_tokens": 10,
        "output_tokens": 10,
        "web_searches": 3,
        "cost_usd": 0.03,
        "cost_source": "оценка",
    }
    with (demo / "cost.jsonl").open("ab") as f:
        f.write((json.dumps(line) + "\n").encode())
    from helpers import rehash

    rehash(demo)
    d = report(cli, "--json")
    assert d["web_search"]["calls"] == 3 and d["web_search"]["usd"] == pytest.approx(0.03)


@pytest.mark.parametrize(
    ("calls", "word"),
    [
        (0, "вызовов"),
        (1, "вызов"),
        (2, "вызова"),
        (4, "вызова"),
        (5, "вызовов"),
        (11, "вызовов"),
        (12, "вызовов"),
        (21, "вызов"),
        (22, "вызова"),
        (25, "вызовов"),
        (101, "вызов"),
        (111, "вызовов"),
    ],
)
def test_web_search_line_declines_the_calls(calls, word):
    from strateg import costreport

    assert costreport._web_search(calls, 0.5) == f"веб-поиск: {calls} {word}, $0,5000 (оценка)"


def test_web_search_line_in_the_report_has_the_right_form_for_one_and_for_twenty_one_calls(
    cli, demo, tmp_path
):
    main, _ = session_files(tmp_path / "d", "s1")
    write(main, assistant("a1", SONNET, out=0, tools=searches("t1")))
    accounting.account(demo, "s1", [src("main", main, "основной")])
    assert "веб-поиск: 1 вызов, $0,0117 (оценка); входит в сумму выше." in report(cli).splitlines()
    write(
        main,
        assistant("a1", SONNET, out=0, tools=searches("t1")),
        assistant("a2", SONNET, out=0, tools=searches(*[f"u{n}" for n in range(20)])),
    )
    accounting.account(demo, "s1", [src("main", main, "основной")])
    assert "веб-поиск: 21 вызов, " in report(cli)


def test_report_says_the_search_model_has_no_price_when_the_table_has_no_query(
    cli, demo, tmp_path, monkeypatch
):
    """Таблица цен без `web_search_query`: поиск — одна плата, а отчёт говорит, что запрос не оценён."""
    import yaml

    from strateg import budget

    raw = yaml.safe_load((budget.config_dir() / "prices.yaml").read_text(encoding="utf-8"))
    del raw["web_search_query"]
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "prices.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    shutil.copy(budget.config_dir() / "defaults.yaml", folder / "defaults.yaml")
    monkeypatch.setenv(budget.CONFIG_ENV, str(folder))
    fill_searching(demo, tmp_path)  # шесть поисков, каждый по одной плате
    d = report(cli, "--json")
    assert d["web_search"]["query_priced"] is False
    assert d["web_search"]["calls"] == 6 and d["web_search"]["usd"] == pytest.approx(0.06, abs=1e-6)
    line = "веб-поиск: 6 вызовов, $0,0600 (оценка); модель поиска без цены; входит в сумму выше."
    assert line in report(cli).splitlines()
