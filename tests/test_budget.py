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


@pytest.fixture(autouse=True)
def five_dollars(request, tmp_path, monkeypatch):
    """Тесты ходов бюджета считают от $5 и 60 минут, а не от числа по умолчанию: оно меняется по замерам.

    Тесты без `cli` читают настоящие `config/defaults.yaml` и `config/prices.yaml` и этим не затронуты.
    """
    if "cli" not in request.fixturenames:
        return
    folder = tmp_path / "pinned-config"
    folder.mkdir()
    defaults = yaml.safe_load((REPO / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    defaults["budget"]["usd"] = 5
    (folder / "defaults.yaml").write_text(yaml.safe_dump(defaults), encoding="utf-8")
    (folder / "prices.yaml").write_bytes((REPO / "config" / "prices.yaml").read_bytes())
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))


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
    # по замеру сквозного прогона с субагентами: $5,23 и 25 минут до выпуска без повторной проверки полноты
    assert (d.usd, d.minutes, d.warn_share) == (8, 60, 0.8)
    assert (d.release_reserve.usd, d.release_reserve.minutes) == (0.5, 5)
    assert d.usd > 5.23 + 0.7 + 0.5 and d.minutes > 25 * 2  # выпуск, повторная проверка полноты и запас
    text = (REPO / "config" / "defaults.yaml").read_text(encoding="utf-8")
    assert "сквозного прогона" in text and "$5,23" in text  # основание числа — в комментарии


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
        "  release_reserve:\n    usd: 1\n    minutes: 2\n"
        "follow_up:\n  source_max_age_days: 90\n  commitments_window_days: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    new(cli)
    assert load_yaml(home / "demo" / "state.yaml")["budget"] == {
        "limit_usd": 9.0,
        "limit_minutes": 20.0,
        "spent_minutes": 0.0,
    }


def test_old_defaults_without_follow_up_still_work(cli, home, tmp_path, monkeypatch):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "defaults.yaml").write_text(
        "budget:\n  usd: 9\n  minutes: 20\n  warn_share: 0.5\n"
        "  release_reserve:\n    usd: 1\n    minutes: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    follow = budgetlib.load_defaults().follow_up
    assert (follow.source_max_age_days, follow.commitments_window_days) == (180, 7)
    new(cli)
    code, out, err = cli("due", "--project", "demo")
    assert code == 0, err


def test_broken_defaults_are_a_clear_error(cli, home, tmp_path, monkeypatch):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "defaults.yaml").write_text("budget:\n  usd: -1\n", encoding="utf-8")
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    code, _, err = cli("new", "demo")
    assert code == 1 and "defaults.yaml" in err


def test_prices_file_is_valid_dated_and_complete():
    prices = budgetlib.load_prices()
    assert prices.accessed == "2026-11-08" and prices.source.endswith("/about-claude/pricing")
    for model in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
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


# --- budget check --next: цена следующего шага (Д-5) -------------------------------------------------


def add_cost(cli, usd: float) -> None:
    code, _, err = cli("cost", "add", "--session", "s1", "--role", "r", "--model", "m", "--usd", str(usd))
    assert code == 0, err


def step_cost(cli, clock, step: str, usd: float, minutes: float = 1) -> None:
    """Шаг с расходом: сообщения учитываются на шаге, который идёт, — так их видит оценка по истории."""
    assert cli("step", "start", step)[0] == 0
    add_cost(cli, usd)
    clock.advance(minutes * 60)
    assert cli("step", "done", step)[0] == 0


def history(cli, clock, usd: dict[str, float], minutes: float = 1) -> None:
    for step, value in usd.items():
        step_cost(cli, clock, step, value, minutes)


def next_estimate(cli, *argv: str) -> dict:
    return json.loads(cli("budget", "check", "--next", *argv, "--json")[1])["estimate"]


def test_step_estimate_table_is_in_the_defaults_file_and_matches_the_code():
    d = budgetlib.load_defaults()
    raw = yaml.safe_load((REPO / "config" / "defaults.yaml").read_text(encoding="utf-8"))["step_estimate"]
    assert list(raw) == list(budgetlib.STEPS) == list(d.step_estimate)
    assert {k: (v["usd"], v["minutes"]) for k, v in raw.items()} == budgetlib.DEFAULT_STEP_ESTIMATE
    # шаги до выпуска вместе — порядка замера сквозного прогона ($5,23 и около 25 минут)
    before_release = [v for k, v in budgetlib.DEFAULT_STEP_ESTIMATE.items() if k != "8"]
    assert 4.5 <= sum(u for u, _ in before_release) <= 6.5 and 20 <= sum(m for _, m in before_release) <= 35


def test_a_config_without_the_estimate_section_still_loads(tmp_path, monkeypatch):
    folder = tmp_path / "old-config"
    folder.mkdir()
    old = "budget:\n  usd: 8\n  minutes: 60\n  warn_share: 0.8\n  release_reserve: {usd: 0.5, minutes: 5}\n"
    (folder / "defaults.yaml").write_text(old, encoding="utf-8")
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(folder))
    assert budgetlib.load_defaults().step_estimate["2б"].usd == 1.6


def test_next_without_history_uses_the_table_and_says_it_is_an_estimate(cli, home):
    new(cli)
    code, out, _ = cli("budget", "check", "--next")
    assert code == 0 and "позволяет шаг 1" in out
    assert (
        "оценка шага 1 (оценка): $0,50 и 2,0 мин" in out and "оценка по таблице config/defaults.yaml" in out
    )
    est = next_estimate(cli, "2б")
    assert (
        est["step"] == "2б" and est["usd"] == 1.6 and est["estimated"] is True and "таблице" in est["source"]
    )


def test_next_refuses_when_the_rest_does_not_fit_the_next_step_while_plain_check_allows(cli, home, clock):
    """После шага 4 остаток меньше цены шага 5: обычный `budget check` позволяет, `--next` называет цену."""
    new(cli)
    history(cli, clock, {"1": 0.75, "2а": 0.5, "2б": 2.0, "3": 0.4, "4": 0.9})  # потрачено $4,55 из $5
    assert cli("budget", "check")[0] == 0
    code, out, err = cli("budget", "check", "--next")
    assert code == 1 and out == ""
    assert "бюджета не хватит на шаг 5 (деньги)" in err and "шаг не начинать" in err
    assert "осталось $0,45" in err and "оценка шага 5: $0,75" in err
    assert "медиана 5 завершённых шагов проекта: 1, 2а, 2б, 3, 4" in err and "(оценка)" not in err
    assert "budget set" in err
    (event,) = budget_events(home / "demo")
    assert "бюджета не хватит на шаг 5" in event["message"] and "$0,45" in event["message"]
    data = json.loads(cli("budget", "check", "--next", "--json")[1])
    assert data["ok"] is False and data["allowed"] is False and data["estimate"]["step"] == "5"


def test_next_names_the_step_by_status_after_a_done_step_and_the_same_step_while_it_runs(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 0.1, "2а": 0.1})
    assert next_estimate(cli)["step"] == "2б"
    assert cli("step", "start", "2б")[0] == 0
    assert next_estimate(cli)["step"] == "2б"
    assert cli("step", "done", "2б")[0] == 0
    assert next_estimate(cli)["step"] == "3"


def test_next_after_the_last_step_prices_the_review_again(cli, home, clock):
    new(cli)
    for step in ("1", "2а", "2б", "3", "4", "5", "6", "7", "8"):
        work(cli, clock, 0.1, step=step)
    assert next_estimate(cli)["step"] == "8"


def test_next_estimate_is_the_median_of_the_done_steps_and_a_big_step_does_not_skew_it(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 0.2, "2а": 0.3, "2б": 3.0})  # медиана $0,30, а не среднее $1,17
    est = next_estimate(cli)
    assert est["usd"] == 0.3 and est["estimated"] is False


def test_next_allows_when_the_rest_equals_the_estimate_and_refuses_below_it(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 0.6, "2а": 0.6, "2б": 0.6})  # медиана $0,60
    add_cost(cli, 2.6)  # потрачено $4,40, остаток $0,60
    assert cli("budget", "check", "--next")[0] == 0
    add_cost(cli, 0.01)
    assert cli("budget", "check", "--next")[0] == 1


def test_next_refuses_by_time_with_the_time_estimate_from_the_journal(cli, home, clock):
    new(cli)
    work(cli, clock, 25, step="1")
    work(cli, clock, 25, step="2а")  # остаток 10 мин, шаг в среднем — 25
    code, _, err = cli("budget", "check", "--next")
    assert code == 1 and "бюджета не хватит на шаг 2б (время)" in err
    assert "осталось 10,0 мин" in err and "оценка шага 2б" in err and "25,0 мин" in err


def test_next_without_a_price_looks_at_the_time_only(cli, home, clock):
    new(cli)
    work(cli, clock, 1, step="1")  # стоимость неизвестна: деньги не считаются
    code, out, _ = cli("budget", "check", "--next")
    assert code == 0 and "позволяет шаг 2а" in out
    assert json.loads(cli("budget", "check", "--next", "--json")[1])["budget"]["remaining_usd"] is None


def test_release_reserve_counts_for_the_next_step_of_a_release(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 0.9, "2а": 0.9, "2б": 0.9, "3": 0.9, "4": 0.9})  # $4,50, остаток $0,50
    assert cli("budget", "check", "--next", "7")[0] == 1  # шаг 7 — по медиане $0,90
    code, out, _ = cli("budget", "check", "--next", "7", "--action", "выпуск")
    assert code == 0 and "позволяет шаг 7" in out  # остаток с резервом $1,00


def test_next_does_not_hide_an_exhausted_budget(cli, home):
    new(cli)
    cost(cli, 6)
    code, _, err = cli("budget", "check", "--next")
    assert code == 1 and "бюджет исчерпан (деньги)" in err and "не хватит" not in err


def test_a_refusal_for_the_next_step_is_written_once_until_the_budget_changes(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 1.0, "2а": 1.0, "2б": 1.0, "3": 1.0})  # медиана $1,00
    add_cost(cli, 0.2)  # остаток $0,80
    for _ in range(3):
        assert cli("budget", "check", "--next")[0] == 1
    assert len(budget_events(home / "demo")) == 1
    assert cli("budget", "set", "--usd", "6")[0] == 0
    assert cli("budget", "check", "--next")[0] == 0


@pytest.mark.parametrize("step", ["9", "шаг", "0"])
def test_next_with_an_unknown_step_is_a_usage_error(cli, home, step):
    new(cli)
    code, _, err = cli("budget", "check", "--next", step)
    assert code == 2 and "не бывает" in err
    assert budget_events(home / "demo") == []


def test_the_rules_of_the_agent_point_at_next():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    line = next(x for x in agents.splitlines() if x.startswith("- бюджет израсходован:"))
    assert "budget check --next" in line and "на границе шагов" in line and "код 1" in line
    completeness = (REPO / ".claude" / "skills" / "strategy" / "completeness.md").read_text(encoding="utf-8")
    assert "budget check --next" in completeness


def test_then_adds_the_next_step_to_the_price_so_a_repeat_keeps_the_reserve_for_the_release(cli, home, clock):
    """Остаток хватает на повтор шага 6, но не на повтор и выпуск (шаг 7) вместе."""
    new(cli)
    history(cli, clock, {"1": 0.6, "2а": 0.6, "2б": 0.6})  # медиана $0,60 на шаг
    add_cost(cli, 2.3)  # потрачено $4,10, остаток $0,90
    assert cli("budget", "check", "--next", "6")[0] == 0  # на один шаг хватает
    code, out, err = cli("budget", "check", "--next", "6", "--then", "7")
    assert code == 1 and out == ""
    assert "бюджета не хватит на шаги 6 и 7 (деньги)" in err and "оценка шага 6 и 7: $1,20" in err
    data = json.loads(cli("budget", "check", "--next", "6", "--then", "7", "--json")[1])
    assert data["allowed"] is False and data["estimate"]["usd"] == 1.2 and data["estimate"]["step"] == "6 и 7"


def test_then_allows_when_both_steps_fit_and_needs_next(cli, home, clock):
    new(cli)
    history(cli, clock, {"1": 0.6, "2а": 0.6, "2б": 0.6})  # потрачено $1,80
    code, out, _ = cli("budget", "check", "--next", "6", "--then", "7")
    assert code == 0 and "Бюджет позволяет шаги 6 и 7" in out
    assert cli("budget", "check", "--then", "7")[0] == 2  # --then без --next — ошибка применения


def test_then_adds_the_minutes_too(cli, home, clock):
    """Остаток минут между ценой одного и двух шагов: `--next 6` проходит, с `--then 7` — нет (время)."""
    new(cli)
    history(cli, clock, {"1": 0.1, "2а": 0.1, "2б": 0.1}, minutes=10)  # медиана 10 мин на шаг
    assert cli("budget", "set", "--minutes", "45")[0] == 0  # потрачено 30 мин, остаток 15
    assert cli("budget", "check", "--next", "6")[0] == 0
    code, _, err = cli("budget", "check", "--next", "6", "--then", "7")
    assert code == 1 and "шаги 6 и 7 (время)" in err and "(деньги)" not in err
    data = json.loads(cli("budget", "check", "--next", "6", "--then", "7", "--json")[1])
    assert data["estimate"]["minutes"] == 20


@pytest.mark.parametrize(("nxt", "then"), [("7", "6"), ("6", "6"), ("6", "2а")])
def test_then_must_name_a_later_step_than_next(cli, home, nxt, then):
    new(cli)
    code, _, err = cli("budget", "check", "--next", nxt, "--then", then)
    assert code == 2 and "--then" in err
