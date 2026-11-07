"""Бюджет проекта (ADR-0010): `config/defaults.yaml`, `config/prices.yaml`, `budget set`, `budget check`."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from helpers import journal_of, load_yaml
from strateg import budget as budgetlib

REPO = Path(__file__).resolve().parent.parent


def new(cli) -> None:
    assert cli("new", "demo")[0] == 0


def cost(cli, usd: float, session: str = "s1") -> None:
    code, _, err = cli(
        "cost", "add", "--session", session, "--role", "r", "--model", "m", "--usd", str(usd), "--total"
    )
    assert code == 0, err


def work(cli, clock, minutes: float, step: str = "2б") -> None:
    assert cli("step", "start", step)[0] == 0
    clock.advance(minutes * 60)
    assert cli("step", "done", step)[0] == 0


def budget_events(root: Path) -> list[dict]:
    return [e for e in journal_of(root) if e["kind"] == "бюджет"]


# --- config/defaults.yaml и config/prices.yaml -------------------------------------------------------


def test_defaults_file_is_valid_and_has_the_chosen_numbers():
    d = budgetlib.load_defaults().budget
    # по замеру двух прогонов (13–15 минут, около 1,7 доллара) с запасом
    assert (d.usd, d.minutes, d.warn_share) == (5, 60, 0.8)
    assert (d.release_reserve.usd, d.release_reserve.minutes) == (0.5, 5)
    assert d.usd > 1.7 * 2 and d.minutes > 15 * 2


def test_new_project_gets_the_default_budget(cli, home):
    new(cli)
    state = load_yaml(home / "demo" / "state.yaml")
    d = budgetlib.load_defaults().budget
    assert state["budget"]["limit_usd"] == d.usd and state["budget"]["limit_minutes"] == d.minutes
    assert state["budget"]["spent_minutes"] == 0


def test_defaults_are_taken_from_the_config_folder(cli, home, tmp_path, monkeypatch):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "defaults.yaml").write_text(
        "budget:\n  usd: 9\n  minutes: 20\n  warn_share: 0.5\n"
        "  release_reserve:\n    usd: 1\n    minutes: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    new(cli)
    assert load_yaml(home / "demo" / "state.yaml")["budget"] == {
        "limit_usd": 9.0,
        "limit_minutes": 20.0,
        "spent_minutes": 0.0,
    }


def test_broken_defaults_are_a_clear_error(cli, home, tmp_path, monkeypatch):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "defaults.yaml").write_text("budget:\n  usd: -1\n", encoding="utf-8")
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    code, _, err = cli("new", "demo")
    assert code == 1 and "defaults.yaml" in err


def test_prices_file_is_valid_dated_and_complete():
    prices = budgetlib.load_prices()
    assert prices.accessed == "2026-11-07" and prices.source.endswith("/about-claude/pricing")
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert model in prices.models, model
    assert prices.web_search_usd_per_1000 == 10
    assert prices.batch_multiplier == 0.5 and prices.us_data_residency_multiplier == 1.1
    assert "claude-opus-5-5" in prices.fast_mode


def test_prices_follow_the_cache_multipliers_of_the_price_page():
    prices = budgetlib.load_prices()
    for name, p in prices.models.items():
        assert p.cache_write_5m == pytest.approx(p.input * 1.25), name
        assert p.cache_write_1h == pytest.approx(p.input * 2), name
    # чтение кэша: 0,05 цены ввода у Opus 5.5 и Sonnet 5.5, 0,025 у Fable 5.1, у остальных 0,1
    assert prices.models["claude-opus-5-5"].cache_read == pytest.approx(0.05 * 4)
    assert prices.models["claude-sonnet-5-5"].cache_read == pytest.approx(0.05 * 2)
    assert prices.models["claude-fable-5-1"].cache_read == pytest.approx(0.025 * 10)
    haiku = prices.models["claude-haiku-5-5"]
    assert haiku.cache_read == pytest.approx(0.1 * haiku.input)
    assert haiku.long_prompt is not None and haiku.long_prompt.threshold_tokens == 100_000
    assert haiku.long_prompt.input == pytest.approx(5 * haiku.input)


def test_prices_file_has_no_numbers_without_a_date():
    text = (REPO / "config" / "prices.yaml").read_text(encoding="utf-8")
    assert text.count('accessed: "') == 1
    assert dt.date.fromisoformat(yaml.safe_load(text)["accessed"]) <= dt.date.today()


# --- budget set --------------------------------------------------------------------------------------


def test_budget_set_changes_limits_and_writes_an_event(cli, home):
    new(cli)
    code, out, err = cli("budget", "set", "--usd", "8", "--minutes", "90")
    assert code == 0, err
    assert "$5,00 -> $8,00" in out and "60 -> 90 мин" in out
    state = load_yaml(home / "demo" / "state.yaml")["budget"]
    assert state["limit_usd"] == 8 and state["limit_minutes"] == 90
    events = budget_events(home / "demo")
    assert len(events) == 1 and "бюджет изменён" in events[0]["message"]
    assert cli("check")[0] == 0


def test_budget_set_changes_each_limit_separately(cli, home):
    new(cli)
    assert cli("budget", "set", "--minutes", "45")[0] == 0
    state = load_yaml(home / "demo" / "state.yaml")["budget"]
    assert state["limit_usd"] == 5 and state["limit_minutes"] == 45
    assert cli("budget", "set", "--usd", "7.5")[0] == 0
    assert load_yaml(home / "demo" / "state.yaml")["budget"]["limit_usd"] == 7.5


def test_new_limit_is_shown_in_the_status_line(cli, home):
    new(cli)
    cost(cli, 1.2)
    assert "$1,20 из $5" in cli("status", "--line")[1]
    assert cli("budget", "set", "--usd", "8", "--minutes", "90")[0] == 0
    line = cli("status", "--line")[1].strip()
    assert "$1,20 из $8" in line and line.endswith("0 из 90 мин")


def test_budget_set_without_change_writes_nothing(cli, home):
    new(cli)
    assert cli("budget", "set", "--usd", "5", "--minutes", "60")[0] == 0
    code, out, _ = cli("budget", "set", "--usd", "5")
    assert code == 0 and "уже такой" in out
    assert budget_events(home / "demo") == []


@pytest.mark.parametrize(
    "argv",
    [
        ("budget", "set"),
        ("budget", "set", "--usd", "0"),
        ("budget", "set", "--usd", "-3"),
        ("budget", "set", "--minutes", "nan"),
        ("budget", "set", "--usd", "inf"),
        ("budget", "set", "--usd", "много"),
    ],
)
def test_budget_set_bad_usage_is_code_2(cli, home, argv):
    new(cli)
    assert cli(*argv)[0] == 2
    assert load_yaml(home / "demo" / "state.yaml")["budget"]["limit_usd"] == 5
    assert budget_events(home / "demo") == []


# --- budget check ------------------------------------------------------------------------------------


def test_check_allows_while_the_budget_lasts(cli, home):
    new(cli)
    code, out, _ = cli("budget", "check", "--action", "fetch")
    assert code == 0 and "позволяет «fetch»" in out
    assert budget_events(home / "demo") == []
    data = json.loads(cli("budget", "check", "--json")[1])
    assert data["allowed"] is True and data["budget"]["remaining_minutes"] == 60


def test_check_refuses_when_money_is_spent(cli, home):
    new(cli)
    cost(cli, 5.2)
    code, out, err = cli("budget", "check", "--action", "fetch")
    assert code == 1 and out == ""
    assert "бюджет исчерпан (деньги)" in err and "«fetch»" in err and "budget set" in err
    events = budget_events(home / "demo")
    assert len(events) == 1
    assert "исчерпан" in events[0]["message"] and "деньги" in events[0]["message"]
    assert cli("check")[0] == 0


def test_check_refuses_when_time_is_spent(cli, home, clock):
    new(cli)
    work(cli, clock, 61)
    code, _, err = cli("budget", "check")
    assert code == 1 and "бюджет исчерпан (время)" in err
    assert "время" in budget_events(home / "demo")[0]["message"]


def test_check_counts_the_running_step(cli, home, clock):
    new(cli)
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(59 * 60)
    assert cli("budget", "check")[0] == 0
    clock.advance(2 * 60)
    assert cli("budget", "check")[0] == 1


def test_check_names_both_reasons(cli, home, clock):
    new(cli)
    cost(cli, 6)
    work(cli, clock, 70)
    code, out, _ = cli("budget", "check", "--json")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False and data["allowed"] is False
    assert data["reason"] == "деньги и время"
    assert "деньги и время" in cli("budget", "check")[2]


def test_unknown_cost_limits_only_the_time(cli, home, clock):
    new(cli)
    code, out, _ = cli("budget", "check")
    assert code == 0 and "деньги не считаются" in out
    work(cli, clock, 59)
    assert cli("budget", "check")[0] == 0
    work(cli, clock, 2, step="3")
    code, _, err = cli("budget", "check")
    assert code == 1 and "(время)" in err and "(деньги" not in err


def test_waiting_for_an_answer_at_the_gate_is_not_spent_time(cli, home, clock):
    new(cli)
    assert cli("step", "start", "2а")[0] == 0
    clock.advance(5 * 60)
    assert cli("gate", "open", "В2", "--questions", "2")[0] == 0
    clock.advance(3 * 3600)  # человек думает три часа
    assert cli("gate", "answer", "В2", "--answer", "ок")[0] == 0
    assert cli("step", "done", "2а")[0] == 0
    assert cli("budget", "check")[0] == 0
    data = json.loads(cli("budget", "check", "--json")[1])
    assert data["budget"]["spent_minutes"] == 5


def test_release_may_use_the_reserve(cli, home):
    new(cli)
    cost(cli, 5.3)  # лимит $5, резерв на выпуск ещё $0,5
    assert cli("budget", "check")[0] == 1
    code, out, _ = cli("budget", "check", "--action", "выпуск")
    assert code == 0 and "«выпуск»" in out
    cost(cli, 5.6)
    assert cli("budget", "check", "--action", "выпуск")[0] == 1
    assert cli("budget", "check", "--action", "release")[0] == 1


def test_repeated_refusals_do_not_flood_the_journal(cli, home):
    new(cli)
    cost(cli, 6)
    for _ in range(3):
        assert cli("budget", "check", "--action", "fetch")[0] == 1
    assert len(budget_events(home / "demo")) == 1
    assert cli("budget", "check", "--action", "claim add")[0] == 1
    assert len(budget_events(home / "demo")) == 2


def test_a_refusal_is_written_once_while_the_time_keeps_growing(cli, home, clock):
    new(cli)
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(61 * 60)
    for _ in range(5):
        assert cli("budget", "check", "--action", "fetch")[0] == 1
        clock.advance(30)  # шаг идёт, потрачено минут становится больше
    assert len(budget_events(home / "demo")) == 1


def test_a_refusal_is_not_repeated_after_another_event_in_between(cli, home):
    new(cli)
    cost(cli, 6)
    for _ in range(3):
        assert cli("budget", "check", "--action", "fetch")[0] == 1
        assert cli("event", "error", "--message", "сбой сети")[0] == 0
    assert len(budget_events(home / "demo")) == 1


def test_changing_the_limit_starts_the_refusals_again(cli, home):
    new(cli)
    cost(cli, 6)
    assert cli("budget", "check", "--action", "fetch")[0] == 1
    assert cli("budget", "set", "--usd", "5.5")[0] == 0
    assert cli("budget", "check", "--action", "fetch")[0] == 1
    assert len(budget_events(home / "demo")) == 3  # отказ, изменение, отказ


# --- границы: ровно 80 % и ровно 100 % ----------------------------------------------------------------


def test_exactly_the_money_limit_is_a_refusal_and_just_below_is_not(cli, home):
    new(cli)
    cost(cli, 4.99)
    assert cli("budget", "check")[0] == 0
    cost(cli, 5)
    assert cli("budget", "check")[0] == 1
    assert "бюджет исчерпан: деньги" in cli("status", "--line")[1]


def test_exactly_the_time_limit_is_a_refusal_and_just_below_is_not(cli, home, clock):
    new(cli)
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(59 * 60 + 59)
    assert cli("budget", "check")[0] == 0
    clock.advance(1)
    assert cli("budget", "check")[0] == 1
    assert "бюджет исчерпан: время" in cli("status", "--line")[1]


def test_warning_starts_at_exactly_80_percent_of_money(cli, home):
    new(cli)
    cost(cli, 3.99)
    assert "бюджета" not in cli("status", "--line")[1]
    cost(cli, 4)
    assert "израсходовано 80 % бюджета (деньги)" in cli("status", "--line")[1]
    assert cli("budget", "check")[0] == 0


def test_warning_starts_at_exactly_80_percent_of_time(cli, home, clock):
    new(cli)
    assert cli("step", "start", "2б")[0] == 0
    clock.advance(48 * 60 - 1)
    assert "бюджета" not in cli("status", "--line")[1]
    clock.advance(1)
    assert "израсходовано 80 % бюджета (время)" in cli("status", "--line")[1]


def test_adding_budget_after_the_stop_lets_work_continue(cli, home):
    new(cli)
    cost(cli, 5.2)
    assert cli("stop", "--reason", "бюджет исчерпан")[0] == 0
    assert cli("budget", "check")[0] == 1
    assert cli("budget", "set", "--usd", "8")[0] == 0
    assert cli("budget", "check")[0] == 0
    assert "$5,20 из $8" in cli("status", "--line")[1]
    kinds = [e["kind"] for e in journal_of(home / "demo") if e["kind"] == "бюджет"]
    assert kinds == ["бюджет", "бюджет"]  # отказ и изменение лимита
    assert "Внеплановая остановка" not in (load_yaml(home / "demo" / "state.yaml")["next_action"] or "")


# --- остаток и предупреждение в статусе -------------------------------------------------------------


def test_status_and_brief_show_what_is_left(cli, home, clock):
    new(cli)
    cost(cli, 1.5)
    work(cli, clock, 10)
    out = cli("status")[1]
    assert "Бюджет: осталось $3,50 из $5 и 50,0 из 60 мин" in out
    brief = cli("status", "--brief", "--peek")[1]
    assert "Бюджет: осталось $3,50 из $5 и 50,0 из 60 мин" in brief
    assert len(brief.strip().splitlines()) <= 10
    data = json.loads(cli("status", "--json")[1])["budget"]
    assert data["remaining_usd"] == 3.5 and data["remaining_minutes"] == 50 and data["exhausted"] == []


def test_status_line_warns_at_80_percent_and_says_when_it_is_over(cli, home, clock):
    new(cli)
    cost(cli, 3.9)
    assert "бюджета" not in cli("status", "--line")[1]
    cost(cli, 4.2)
    assert "израсходовано 84 % бюджета (деньги)" in cli("status", "--line")[1]
    cost(cli, 5.1)
    assert "бюджет исчерпан: деньги" in cli("status", "--line")[1]


def test_status_line_warns_by_time_too(cli, home, clock):
    new(cli)
    work(cli, clock, 50)
    assert "израсходовано 83 % бюджета (время)" in cli("status", "--line")[1]
