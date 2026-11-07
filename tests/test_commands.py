"""Команды CLI: сквозной путь, отказы, идемпотентность, шаги, ворота, стоимость, строка статуса."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from helpers import PUBLISHER, QUOTE, URL, claims_of, fact, put_frame, write_claims  # noqa: F401
from strateg import project as proj
from strateg.project_check import check_project

# --- Сквозной путь ---


def test_end_to_end_case(cli, net, page_html, home, tmp_path, clock):
    net.add(URL, page_html)

    code, out, _ = cli("new", "demo", "--mode", "пилот")
    assert code == 0 and "создан" in out
    assert (home / ".active").read_text(encoding="utf-8") == "demo\n"
    for folder in ("inputs", "sources", "outputs"):
        assert (home / "demo" / folder).is_dir()
    for name in ("state.yaml", "sources.yaml", "claims.yaml", "journal.jsonl", "cost.jsonl"):
        assert (home / "demo" / name).is_file()

    assert cli("step", "start", "1")[0] == 0
    clock.advance(30)
    code, out, err = cli("fetch", URL, "--region", "RU-MOW")
    assert code == 0, err
    assert "И-1" in out
    assert (home / "demo" / "sources" / "i-1.txt").is_file()

    claims = [
        fact(),
        {
            "text": "Подключают защиту чаще всего семьи с пожилыми родителями",
            "type": "гипотеза",
            "subject": "кто подключает защиту",
            "basis": ["У-1"],
            "check_method": "спросить в интервью пятерых подписчиков",
        },
        {
            "text": "Цена на сайте не изменится до конца 2026 года",
            "type": "допущение",
            "subject": "цена подписки «Защита»",
            "taken_from": "слова руководителя продукта",
            "origin": "пользователь",
        },
    ]
    # У-1 нужен гипотезе как основание: сначала факт, потом остальные
    path = write_claims(tmp_path, claims[:1], "a.yaml")
    code, out, err = cli("claim", "add", "--file", path)
    assert code == 0, err
    assert "У-1: записано" in out
    code, out, err = cli("claim", "add", "--file", write_claims(tmp_path, claims[1:], "b.yaml"))
    assert code == 0, err
    assert "У-2" in out and "У-3" in out

    put_frame(cli)
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    clock.advance(600)  # человек отвечает десять минут: в рабочее время не входит
    assert cli("gate", "answer", "В1", "--answer", "1а, ок")[0] == 0
    clock.advance(30)
    assert cli("step", "done", "1", "--next", "Начать шаг 2а")[0] == 0

    code, out, _ = cli("status", "--json")
    data = json.loads(out)
    assert data["gates"]["В1"] == "отвечено"
    assert data["sources"]["total"] == 1 and data["claims"]["by_type"] == {
        "факт": 1,
        "гипотеза": 1,
        "допущение": 1,
    }
    assert data["work_minutes"] == pytest.approx(1.0, abs=0.01)  # 30 + 30 секунд без десяти минут ожидания
    assert data["next_action"] == "Начать шаг 2а"

    code, out, _ = cli("status")
    assert code == 0 and "Проект: demo" in out and "Шаг: 1" in out

    code, out, err = cli("check")
    assert code == 0 and "ошибок нет" in out, err
    assert check_project(home / "demo") == []

    code, out, _ = cli("claim", "list", "--type", "факт")
    assert code == 0 and "У-1" in out and "У-2" not in out


def test_active_project_flag_and_env(cli, home, monkeypatch):
    cli("new", "first")
    cli("new", "second")
    assert (home / ".active").read_text(encoding="utf-8").strip() == "second"
    assert cli("use", "first")[0] == 0
    assert json.loads(cli("status", "--json")[1])["project"] == "first"
    assert json.loads(cli("status", "--json", "--project", "second")[1])["project"] == "second"
    monkeypatch.setenv(proj.PROJECT_ENV, "second")
    assert json.loads(cli("status", "--json")[1])["project"] == "second"
    assert json.loads(cli("status", "--json", "--project", "first")[1])["project"] == "first"
    monkeypatch.setenv(proj.PROJECT_ENV, str(home / "first"))  # папка проекта вместо номера
    assert json.loads(cli("status", "--json")[1])["project"] == "first"


def test_exit_codes(cli, home):
    code, _, err = cli("status")
    assert code == 1 and "активный проект не выбран" in err
    assert cli("use", "net-takogo")[0] == 1
    assert cli("new", "Плохое Имя")[0] == 2
    code, _, err = cli("step", "start")
    assert code == 2 and "ошибка использования" in err
    cli("new", "demo")
    assert cli("new", "demo")[0] == 1
    assert cli("step", "start", "9")[0] == 2
    assert cli("gate", "open", "В7", "--questions", "1")[0] == 2


def test_latin_letters_from_bash_are_accepted(cli):
    cli("new", "demo")
    assert cli("step", "start", "2b")[0] == 0
    assert cli("gate", "open", "B1", "--questions", "1")[0] == 0
    assert json.loads(cli("status", "--json")[1])["gates"]["В1"] == "ждут ответа"


# --- Идемпотентность ---


def test_repeat_fetch_returns_saved_snapshot(cli, net, page_html, home):
    net.add(URL, page_html)
    cli("new", "demo")
    first = cli("fetch", URL, "--json")
    pages_before = net.urls().count(URL)
    second = cli("fetch", URL, "--json")
    assert pages_before == 1 and net.urls().count(URL) == 1
    a, b = json.loads(first[1]), json.loads(second[1])
    assert a["cached"] is False and b["cached"] is True
    assert a["sha256"] == b["sha256"] and a["source"] == b["source"] == "И-1"
    sources = yaml.safe_load((home / "demo" / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    assert len(sources) == 1


def test_fetch_with_other_region_is_a_new_request(cli, net, page_html):
    net.add(URL, page_html)
    cli("new", "demo")
    cli("fetch", URL)
    code, out, _ = cli("fetch", URL, "--region", "RU-SPE", "--json")
    assert json.loads(out)["source"] == "И-2" and net.urls().count(URL) == 2


def test_fetch_region_defaults_to_project_region(cli, net, page_html, home):
    net.add(URL, page_html)
    cli("new", "demo", "--region", "RU-MOW")
    cli("fetch", URL)
    src = yaml.safe_load((home / "demo" / "sources.yaml").read_text(encoding="utf-8"))["sources"][0]
    assert src["fetch"]["region"] == "RU-MOW"


def test_repeat_claim_add_returns_same_number(started, cli, tmp_path):
    path = write_claims(tmp_path, [fact()])
    first = cli("claim", "add", "--file", path, "--json")
    second = cli("claim", "add", "--file", path, "--json")
    assert json.loads(first[1])["claims"][0] == {
        "id": "У-1",
        "duplicate": False,
        "type": "факт",
        "suspicious": False,
    }
    assert json.loads(second[1])["claims"][0]["id"] == "У-1"
    assert json.loads(second[1])["claims"][0]["duplicate"] is True
    assert len(claims_of(started)) == 1
    done = [
        line
        for line in (started / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if "claim add" in line
    ]
    assert len(done) == 1


def test_claim_from_stdin_and_json(started, cli):
    code, out, err = cli("claim", "add", "--file", "-", stdin=json.dumps(fact(), ensure_ascii=False))
    assert code == 0, err
    assert "У-1" in out


def test_step_start_twice_marks_first_as_interrupted(cli, home):
    cli("new", "demo")
    cli("step", "start", "1")
    code, out, _ = cli("step", "start", "1", "--json")
    assert code == 0 and json.loads(out)["attempt"] == 2 and json.loads(out)["interrupted"] == 1
    events = [
        json.loads(x) for x in (home / "demo" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    steps = [(e["status"], e["idempotency_hash"][:6]) for e in events if e.get("name") == "шаг 1"]
    assert [s for s, _ in steps] == ["начато", "прервано", "начато"]
    assert steps[0][1] == steps[1][1] != steps[2][1]
    assert cli("step", "done", "1")[0] == 0
    assert cli("check")[0] == 0


def test_step_done_twice_and_without_start(cli):
    cli("new", "demo")
    assert cli("step", "done", "1")[0] == 1
    cli("step", "start", "1")
    cli("step", "done", "1")
    code, out, _ = cli("step", "done", "1", "--json")
    assert code == 0 and json.loads(out)["already_done"] is True
    assert cli("check")[0] == 0


def test_step_start_of_other_step_interrupts_open_one(cli, home):
    cli("new", "demo")
    cli("step", "start", "1")
    code, out, _ = cli("step", "start", "2а")
    assert code == 0 and "Шаг 1 не был завершён" in out


# --- Время работы ---


def test_work_time_excludes_gate_waiting(cli, clock):
    cli("new", "demo")
    cli("step", "start", "1")
    clock.advance(120)
    put_frame(cli)
    cli("gate", "open", "В1", "--questions", "2")
    clock.advance(3000)
    cli("gate", "answer", "В1", "--answer", "ок")
    clock.advance(180)
    cli("step", "done", "1")
    data = json.loads(cli("status", "--json")[1])
    assert data["work_minutes"] == pytest.approx(5.0)  # 120 + 180 секунд


def test_unanswered_gate_wait_is_not_work(cli, clock):
    cli("new", "demo")
    cli("step", "start", "1")
    clock.advance(60)
    cli("gate", "open", "В1", "--questions", "1")
    clock.advance(600)
    cli("step", "done", "1")
    assert json.loads(cli("status", "--json")[1])["work_minutes"] == pytest.approx(1.0)


def test_gate_without_questions_passes_automatically(cli):
    cli("new", "demo")
    cli("gate", "open", "В2", "--questions", "0")
    assert json.loads(cli("status", "--json")[1])["gates"]["В2"] == "пройдены автоматически"
    assert cli("check")[0] == 0


def test_gate_rules(cli):
    cli("new", "demo")
    code, _, err = cli("gate", "answer", "В1", "--answer", "ок")
    assert code == 1 and "не ждут ответа" in err
    cli("gate", "open", "В1", "--questions", "1")
    put_frame(cli)
    assert json.loads(cli("gate", "open", "В1", "--questions", "1", "--json")[1])["already_open"] is True
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    assert json.loads(cli("gate", "answer", "В1", "--answer", "ок", "--json")[1])["already_answered"] is True
    assert cli("check")[0] == 0


# --- Стоимость ---


def test_cost_add_and_status(cli, home):
    cli("new", "demo")
    assert json.loads(cli("status", "--json")[1])["cost_usd"] is None
    code, out, _ = cli(
        "cost", "add", "--session", "s1", "--role", "основной агент", "--model", "m", "--usd", "0.5"
    )
    assert code == 0 and "$0,50" in out
    cli(
        "cost",
        "add",
        "--session",
        "s1",
        "--role",
        "исследователь",
        "--model",
        "m",
        "--usd",
        "0.25",
        "--input",
        "100",
    )
    code, out, _ = cli(
        "cost", "add", "--session", "s2", "--role", "основной агент", "--model", "m", "--usd", "1", "--total"
    )
    assert code == 0
    data = json.loads(cli("status", "--json")[1])
    assert data["cost_usd"] == pytest.approx(1.75)
    assert data["cost_precision"] == "оценка"
    cli("cost", "add", "--session", "s1", "--role", "основной агент", "--model", "m", "--usd", "2", "--total")
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(
        3.0
    )  # итог s1 вместо сообщений
    assert cli("check")[0] == 0
    state = yaml.safe_load((home / "demo" / "state.yaml").read_text(encoding="utf-8"))
    assert state["budget"]["spent_usd"] == pytest.approx(3.0)


def test_cost_same_command_twice_is_two_messages_but_explicit_id_is_idempotent(cli):
    cli("new", "demo")
    args = ("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", "0.5")
    cli(*args)
    cli(*args)  # два разных сообщения с одинаковой ценой
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(1.0)
    assert cli(*args, "--message-id", "msg_2")[0] == 0
    assert json.loads(cli(*args, "--message-id", "msg_2", "--json")[1])["duplicate"] is True
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(1.5)
    assert cli("check")[0] == 0


@pytest.mark.parametrize("usd", ["nan", "inf", "-inf", "-0.5"])
def test_cost_usd_must_be_finite_and_not_negative(cli, usd):
    cli("new", "demo")
    code, out, err = cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", usd)
    assert code == 2 and "--usd" in err and "Traceback" not in err + out
    assert json.loads(cli("status", "--json")[1])["cost_usd"] is None


def test_cost_without_message_id_never_merges_identical_messages(cli):
    cli("new", "demo")
    args = ("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", "0.1")
    for _ in range(3):
        assert cli(*args)[0] == 0
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(0.3)


def test_cost_message_id_help_warns_about_repeat(cli):
    code, out, _ = cli("cost", "add", "--help")
    assert code == 0 and "тот же id" in " ".join(out.split()) and "расход удвоится" in " ".join(out.split())


def test_cost_total_without_price_does_not_displace_known_prices(cli):
    cli("new", "demo")
    base = ("cost", "add", "--session", "s1", "--role", "r", "--model", "m")
    cli(*base, "--usd", "1.0", "--input", "10")
    cli(*base, "--usd", "1.5", "--input", "20")
    cli(*base, "--usd", "2.5", "--total")
    cli(*base, "--usd", "0.7", "--input", "30")  # после итога
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(3.2)
    cli(*base, "--total", "--input", "100", "--output", "50")  # итог без цены
    data = json.loads(cli("status", "--json")[1])
    assert data["cost_usd"] == pytest.approx(3.2)
    assert cli("check")[0] == 0


def test_cost_messages_without_totals_are_summed_and_sessions_added(cli):
    cli("new", "demo")
    cli("cost", "add", "--session", "a", "--role", "r", "--model", "m", "--usd", "1")
    cli("cost", "add", "--session", "a", "--role", "r", "--model", "m", "--usd", "2")
    cli("cost", "add", "--session", "b", "--role", "r", "--model", "m", "--usd", "4", "--total")
    assert json.loads(cli("status", "--json")[1])["cost_usd"] == pytest.approx(7.0)


def test_cost_without_price_is_no_data(cli):
    cli("new", "demo")
    code, out, _ = cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--input", "1000")
    assert code == 0 and "данных о цене пока нет" in out
    assert cli("check")[0] == 0
    assert (
        cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--source", "измерено")[0] == 2
    )


def test_cost_measured_source(cli):
    cli("new", "demo")
    cli(
        "cost",
        "add",
        "--session",
        "s1",
        "--role",
        "r",
        "--model",
        "m",
        "--usd",
        "1",
        "--source",
        "измерено",
        "--total",
    )
    assert json.loads(cli("status", "--json")[1])["cost_precision"] == "измерено"


# --- Строка статуса ---

LINE = re.compile(
    r"^Шаг \d[аб]? из 8 · [^·]+ · источников \d+ · утверждений \d+, без цитаты \d+ · сбоев \d+ · "
    r"((\$\d+,\d\d( из \$\d+(,\d+)?)?|стоимость: нет данных) · )?\d+ из \d+ мин$"
)


def test_status_line_format_and_numbers(started, cli, clock):
    cli("step", "start", "2б")
    clock.advance(18 * 60)
    cli("step", "done", "2б")
    cli("claim", "add", "--file", "-", stdin=json.dumps(fact(), ensure_ascii=False))
    cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", "1.2", "--total")
    code, out, _ = cli("status", "--line")
    line = out.strip()
    assert code == 0 and "\n" not in line
    assert (
        line
        == "Шаг 2б из 8 · Сбор · источников 1 · утверждений 1, без цитаты 0 · сбоев 0 · $1,20 · 18 из 60 мин"
    )
    assert json.loads(cli("status", "--line", "--json")[1])["line"] == line


def test_status_line_with_money_limit_and_without_cost(cli, home):
    cli("new", "demo")
    assert cli("status", "--line")[1].strip() == (
        "Шаг 1 из 8 · Постановка · источников 0 · утверждений 0, без цитаты 0 · сбоев 0 · 0 из 60 мин"
    )
    state_file = home / "demo" / "state.yaml"
    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    state["budget"]["limit_usd"] = 5.0
    state_file.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")
    line = cli("status", "--line")[1].strip()
    assert line.endswith("сбоев 0 · стоимость: нет данных · 0 из 60 мин")
    cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", "1.2", "--total")
    line = cli("status", "--line")[1].strip()
    assert line.endswith("сбоев 0 · $1,20 из $5 · 0 из 60 мин")
    assert LINE.match(line)


def test_status_line_counts_failures_and_claims_without_quote(started, cli, net):
    net.add("https://example.com/closed", status=500)
    code, _, _ = cli("fetch", "https://example.com/closed")
    assert code == 1
    line = cli("status", "--line")[1].strip()
    assert "источников 2" in line and "сбоев 1" in line and LINE.match(line)
    data = json.loads(cli("status", "--json")[1])
    assert data["failures"] == 1 and data["sources"]["unavailable"] == 1


def test_status_line_is_one_line_at_each_step(cli):
    cli("new", "demo")
    for step in ("1", "2а", "2б", "3", "4", "5", "6", "7", "8"):
        cli("step", "start", step)
        line = cli("status", "--line")[1].strip()
        assert LINE.match(line), line
