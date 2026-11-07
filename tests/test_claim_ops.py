"""`claim replace`, `claim revoke`, фильтры `claim list`, `step fail`, `event error`."""

from __future__ import annotations

import json
import shutil

import pytest

from helpers import (
    FIXTURE,
    fact,
    fixture_outputs,
    journal_of,
    load_yaml,
    tree_hashes,
    write_claims,
)
from strateg.project_check import check_project

SECOND_QUOTE = (
    "Стоимость подписки в Москве — 199 рублей в месяц. Первые семь дней после подключения "
    "услуга предоставляется бесплатно"
)
THIRD_QUOTE = "Подписка «Защита» помогает отсеивать нежелательные звонки и сообщения от мошенников"


def second(**extra) -> dict:
    return fact(
        SECOND_QUOTE,
        text="Подписка «Защита» в Москве стоит 199 рублей в месяц, первая неделя бесплатно",
        **extra,
    )


def third() -> dict:
    return fact(THIRD_QUOTE, text="Подписка «Защита» помогает отсеивать нежелательные звонки")


def claims(root) -> dict[str, dict]:
    return {c["id"]: c for c in load_yaml(root / "claims.yaml")["claims"]}


@pytest.fixture
def one(started, cli, tmp_path):
    """Проект с одним действующим фактом У-1."""
    assert cli("claim", "add", "--file", write_claims(tmp_path, fact()))[0] == 0
    return started


def replace(cli, tmp_path, old: str, data: dict, *extra: str, name: str = "new.yaml"):
    return cli("claim", "replace", old, "--file", write_claims(tmp_path, data, name), *extra)


# --- claim replace ---


def test_replace_links_both_claims_and_marks_the_old_one(one, cli, tmp_path):
    code, out, err = replace(cli, tmp_path, "У-1", second())
    assert code == 0, err
    assert "У-2" in out and "заменяет У-1" in out
    cs = claims(one)
    assert cs["У-1"]["status"] == "заменено" and cs["У-1"]["replaced_by"] == "У-2"
    assert cs["У-2"]["status"] == "действует" and cs["У-2"]["replaces"] == "У-1"
    last = cs["У-1"]["status_history"][-1]
    assert last["status"] == "заменено" and last["by"] == "агент" and "У-2" in last["reason"]
    assert check_project(one) == []
    event = journal_of(one)[-1]
    assert event["name"] == "strateg claim replace" and "У-1" in event["message"]


def test_replace_chain(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second())[0] == 0
    assert replace(cli, tmp_path, "У-2", third(), name="third.yaml")[0] == 0
    cs = claims(one)
    assert [cs[i]["status"] for i in ("У-1", "У-2", "У-3")] == ["заменено", "заменено", "действует"]
    assert cs["У-1"]["replaced_by"] == "У-2" and cs["У-2"]["replaces"] == "У-1"
    assert cs["У-2"]["replaced_by"] == "У-3" and cs["У-3"]["replaces"] == "У-2"
    assert check_project(one) == []
    live = json.loads(cli("claim", "list", "--status", "действует", "--json")[1])["claims"]
    assert [c["id"] for c in live] == ["У-3"]


def test_repeating_the_same_replacement_returns_the_recorded_number(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second())[0] == 0
    before = tree_hashes(one)
    code, out, err = replace(cli, tmp_path, "У-1", second())
    assert code == 0, err
    assert "У-2" in out and "уже заменено" in out
    assert json.loads(replace(cli, tmp_path, "У-1", second(), "--json")[1])["id"] == "У-2"
    assert tree_hashes(one) == before  # ни нового утверждения, ни события


def test_a_replaced_claim_cannot_be_replaced_by_something_else(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second())[0] == 0
    before = tree_hashes(one)
    code, _, err = replace(cli, tmp_path, "У-1", third(), name="third.yaml")
    assert code == 1 and "уже заменено на У-2" in err
    assert tree_hashes(one) == before


def test_a_revoked_claim_cannot_be_replaced(one, cli, tmp_path):
    assert cli("claim", "revoke", "У-1", "--reason", "неверно")[0] == 0
    code, _, err = replace(cli, tmp_path, "У-1", second())
    assert code == 1 and "отозвано" in err and "только действующее" in err


def test_replace_unknown_claim_and_bad_number(one, cli, tmp_path):
    code, _, err = replace(cli, tmp_path, "У-9", second())
    assert code == 1 and "У-9 нет в реестре" in err
    code, _, err = replace(cli, tmp_path, "Y-1", second())  # латинская буква
    assert code == 2 and "латинские буквы" in err


def test_replace_keeps_subject_period_and_region_or_rolls_back(one, cli, tmp_path):
    before = tree_hashes(one)
    other = second(subject="цена подписки «Защита» в другом городе")
    code, _, err = replace(cli, tmp_path, "У-1", other)
    assert code == 1
    assert "предмет, период или регион" in err
    assert tree_hashes(one) == before  # откат: claims.yaml, state.yaml и журнал прежние


def test_replace_checks_the_new_claim_like_claim_add(one, cli, tmp_path):
    before = tree_hashes(one)
    bad = second()
    bad["quotes"][0]["text"] = "Стоимость подписки в Москве — 299 рублей в месяц"
    code, _, err = replace(cli, tmp_path, "У-1", bad)
    assert code == 1 and "новое утверждение, цитата 1" in err
    assert tree_hashes(one) == before
    code, _, err = replace(cli, tmp_path, "У-1", {"text": "x"})
    assert code == 1 and "новое утверждение" in err and "поле" in err


def test_replace_with_the_same_claim_returns_the_recorded_number(one, cli, tmp_path):
    """Полностью то же утверждение: нового номера нет, прежний не меняется."""
    before = tree_hashes(one)
    code, out, err = replace(cli, tmp_path, "У-1", fact())
    assert code == 0, err
    assert "У-1" in out and "замена не нужна" in out
    assert json.loads(replace(cli, tmp_path, "У-1", fact(), "--json")[1])["id"] == "У-1"
    assert tree_hashes(one) == before
    assert claims(one)["У-1"]["status"] == "действует"


def test_replace_does_not_duplicate_another_claim(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second())[0] == 0  # У-2 действует
    # У-1 заменено, поэтому заменяем У-2 утверждением, которое уже есть как У-1: дубль под новым номером
    code, _, err = replace(cli, tmp_path, "У-2", fact())
    assert code == 1 and "уже записано как У-1" in err


def test_replace_may_differ_only_in_a_field_outside_the_key(started, cli, tmp_path):
    """Гипотеза с другим способом проверки при том же тексте: `claim add` такое не принимает, замена — да."""
    items = [
        fact(),
        {
            "text": "Подключают чаще семьи с пожилыми родителями",
            "type": "гипотеза",
            "subject": "кто подключает защиту",
            "basis": ["У-1"],
            "check_method": "спросить в интервью с пятью семьями",
        },
    ]
    assert cli("claim", "add", "--file", write_claims(tmp_path, items))[0] == 0
    better = {**items[1], "check_method": "опрос 200 абонентов и выгрузка подключений"}
    code, out, err = replace(cli, tmp_path, "У-2", better)
    assert code == 0, err
    cs = claims(started)
    assert cs["У-2"]["status"] == "заменено" and cs["У-2"]["replaced_by"] == "У-3"
    assert cs["У-3"]["check_method"] == "опрос 200 абонентов и выгрузка подключений"
    assert cs["У-3"]["replaces"] == "У-2"
    assert check_project(started) == []
    # повтор той же замены — прежний номер
    assert json.loads(replace(cli, tmp_path, "У-2", better, "--json")[1])["id"] == "У-3"


def test_replace_by_user_is_a_correction(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second(), "--by", "пользователь")[0] == 0
    cs = claims(one)
    assert cs["У-1"]["status_history"][-1]["by"] == "пользователь"
    assert cs["У-2"]["status_history"][0]["by"] == "пользователь"
    events = journal_of(one)[-2:]
    assert [e["kind"] for e in events] == ["действие", "исправление"]
    assert events[1]["actor"] == "пользователь"
    assert check_project(one) == []


def test_replace_from_stdin(one, cli):
    import yaml

    code, out, err = cli(
        "claim", "replace", "У-1", "--file", "-", stdin=yaml.safe_dump(second(), allow_unicode=True)
    )
    assert code == 0, err


# --- claim revoke ---


def test_revoke_changes_status_and_keeps_history(one, cli):
    code, out, err = cli("claim", "revoke", "У-1", "--reason", "человек сказал: это неверно")
    assert code == 0, err
    c = claims(one)["У-1"]
    assert c["status"] == "отозвано"
    assert [h["status"] for h in c["status_history"]] == ["действует", "отозвано"]
    assert c["status_history"][-1]["reason"] == "человек сказал: это неверно"
    assert check_project(one) == []
    assert journal_of(one)[-1]["name"] == "strateg claim revoke"


def test_revoke_twice_says_already_revoked(one, cli):
    assert cli("claim", "revoke", "У-1", "--reason", "неверно")[0] == 0
    before = tree_hashes(one)
    code, out, err = cli("claim", "revoke", "У-1", "--reason", "неверно")
    assert code == 0, err
    assert "уже отозвано" in out
    assert (
        json.loads(cli("claim", "revoke", "У-1", "--reason", "иначе", "--json")[1])["already_revoked"] is True
    )
    assert tree_hashes(one) == before


def test_revoke_of_a_replaced_claim_is_refused(one, cli, tmp_path):
    assert replace(cli, tmp_path, "У-1", second())[0] == 0
    code, _, err = cli("claim", "revoke", "У-1", "--reason", "неверно")
    assert code == 1 and "уже заменено на У-2" in err


def test_revoke_rules(one, cli):
    assert cli("claim", "revoke", "У-1", "--reason", "  ")[0] == 2
    code, _, err = cli("claim", "revoke", "У-7", "--reason", "неверно")
    assert code == 1 and "нет в реестре" in err
    assert cli("claim", "revoke", "У-1")[0] == 2  # причина обязательна


def test_revoke_by_user_is_a_correction(one, cli):
    assert cli("claim", "revoke", "У-1", "--reason", "неверно", "--by", "пользователь")[0] == 0
    assert claims(one)["У-1"]["status_history"][-1]["by"] == "пользователь"
    events = journal_of(one)[-2:]
    assert [e["kind"] for e in events] == ["действие", "исправление"]


@pytest.fixture
def example(home, clock):
    clock.advance(24 * 3600)
    dst = home / "example"
    shutil.copytree(FIXTURE, dst)
    assert check_project(dst) == []
    return dst


def test_revoke_passes_but_names_where_the_claim_is_used(example, cli):
    code, out, err = cli("claim", "revoke", "У-2", "--reason", "цена изменилась", "--project", "example")
    assert code == 0, err  # человек сказал «это неверно»: CLI записывает
    assert claims(example)["У-2"]["status"] == "отозвано"
    assert "Вар-2" in out and "рекомендация" in out
    assert "обещание О-1" in out
    assert "strateg write" in out
    used = json.loads(cli("claim", "revoke", "У-2", "--reason", "x", "--json", "--project", "example")[1])
    assert used["already_revoked"] is True
    # проверка проекта теперь ругается именно на эти места, пока их не перепишут через write
    errors = check_project(example)
    assert errors and all("нельзя держать на отозванном или заменённом" in e for e in errors)


def test_replace_passes_while_old_claim_is_used_and_write_fixes_it(example, cli, tmp_path):
    new = fact(
        "Стоимость подписки в Москве — 199 рублей в месяц. Первые семь дней бесплатно",
        text="Подписка «Защита» в Москве стоит 199 рублей в месяц, первая неделя бесплатно",
    )
    code, out, err = cli(
        "claim", "replace", "У-2", "--file", write_claims(tmp_path, new), "--project", "example"
    )
    assert code == 0, err
    assert "У-7" in out and "рекомендация" in out and "обещание О-1" in out
    assert any("нельзя держать" in e for e in check_project(example))
    # переписать места использования: рекомендация и обещание на новое утверждение
    data = fixture_outputs()
    data["options"]["recommendation"]["claims"] = ["У-7", "У-4"]
    data["promises"]["promises"][0]["claims"] = ["У-7"]
    code, out, err = cli(
        "write",
        "--file",
        "-",
        "--project",
        "example",
        stdin=json.dumps({"options": data["options"], "promises": data["promises"]}, ensure_ascii=False),
    )
    assert code == 0, err
    assert check_project(example) == []


# --- claim list: фильтры по шагу, вопросу плана и статусу ---


def test_claim_list_filters(started, cli, tmp_path):
    assert cli("step", "start", "2б")[0] == 0
    items = [
        fact(plan_question="ВП-1"),
        {
            "text": "Подключают чаще семьи",
            "type": "гипотеза",
            "subject": "кто подключает",
            "basis": ["У-1"],
            "check_method": "интервью",
            "plan_question": "ВП-2",
        },
        {
            "text": "Часть отключит защиту",
            "type": "гипотеза",
            "subject": "отключение",
            "basis": ["У-1"],
            "check_method": "выгрузка",
            "plan_question": "ВП-2",
            "step": "3",
        },
    ]
    assert cli("claim", "add", "--file", write_claims(tmp_path, items))[0] == 0

    def ids(*args):
        code, out, err = cli("claim", "list", "--json", *args)
        assert code == 0, err
        return [c["id"] for c in json.loads(out)["claims"]]

    assert ids() == ["У-1", "У-2", "У-3"]
    assert ids("--step", "2б") == ["У-1", "У-2"]
    assert ids("--step", "3") == ["У-3"]
    assert ids("--step", "2b") == ["У-1", "У-2"]  # латинская b читается как б
    assert ids("--plan-question", "ВП-2") == ["У-2", "У-3"]
    assert ids("--plan-question", "ВП-2", "--step", "2б") == ["У-2"]
    assert ids("--plan-question", "ВП-9") == []
    assert ids("--status", "действует") == ["У-1", "У-2", "У-3"]
    assert ids("--status", "отозвано") == []
    assert cli("claim", "revoke", "У-3", "--reason", "неверно")[0] == 0
    assert ids("--status", "отозвано") == ["У-3"]
    assert ids("--type", "факт") == ["У-1"]
    assert cli("claim", "list", "--step", "9")[0] == 2
    # по шагу и вопросу плана скилл получает записанное, чтобы дописать недостающее
    data = json.loads(cli("claim", "list", "--json", "--plan-question", "ВП-2")[1])["claims"][0]
    assert data["plan_question"] == "ВП-2" and data["attempt"] == 1 and data["basis"] == ["У-1"]


# --- step fail, event error ---


def test_step_fail_marks_the_action(cli, home):
    cli("new", "demo")
    cli("step", "start", "2б")
    code, out, err = cli("step", "fail", "2б", "--reason", "источник недоступен")
    assert code == 0, err
    e = journal_of(home / "demo")[-1]
    assert e["kind"] == "действие" and e["status"] == "ошибка" and e["name"] == "шаг 2б"
    assert e["message"] == "источник недоступен"
    assert json.loads(cli("status", "--json")[1])["failures"] == 1
    assert json.loads(cli("step", "fail", "2б", "--reason", "иначе", "--json")[1])["already_failed"] is True
    assert len(journal_of(home / "demo")) == 3  # повтор события не добавил
    code, _, err = cli("step", "done", "2б")
    assert code == 1 and "не начат заново" in err
    assert cli("step", "start", "2б")[0] == 0  # после ошибки шаг начинают заново
    assert cli("check")[0] == 0


def test_step_fail_rules(cli, home):
    cli("new", "demo")
    code, _, err = cli("step", "fail", "2б", "--reason", "x")
    assert code == 1 and "не начат" in err
    cli("step", "start", "1")
    assert cli("step", "fail", "1", "--reason", " ")[0] == 2
    cli("step", "done", "1")
    code, _, err = cli("step", "fail", "1", "--reason", "x")
    assert code == 1 and "не идёт" in err


def test_event_error(cli, home):
    cli("new", "demo")
    cli("step", "start", "2б")
    code, out, err = cli("event", "error", "--message", "fetch упал по таймауту", "--tool", "Bash")
    assert code == 0, err
    e = journal_of(home / "demo")[-1]
    assert e["kind"] == "ошибка" and e["step"] == "2б" and e["name"] == "Bash" and e["actor"] == "хук"
    assert e["message"] == "fetch упал по таймауту"
    code, _, err = cli("event", "error", "--message", "ошибка скилла", "--step", "1")
    assert code == 0, err
    e = journal_of(home / "demo")[-1]
    assert e["step"] == "1" and e["actor"] == "агент" and "name" not in e
    assert json.loads(cli("status", "--json")[1])["failures"] == 2
    assert cli("event", "error", "--message", " ")[0] == 2
    assert cli("event", "error")[0] == 2
    assert cli("check")[0] == 0


# --- claim list: номер вопроса плана ---


def test_claim_list_plan_question_rejects_latin_look_alikes(started, cli, tmp_path):
    items = [fact(plan_question="ВП-2")]
    assert cli("claim", "add", "--file", write_claims(tmp_path, items))[0] == 0
    for bad in ("VP-2", "ВП-02", "вп-2", "BП-2"):
        code, _, err = cli("claim", "list", "--plan-question", bad)
        assert code == 2, bad
        assert "ВП-1" in err and "номер вопроса плана" in err
    code, _, err = cli("claim", "list", "--plan-question", "VP-2")
    assert "латинские буквы" in err  # подсказка называет латиницу
    # правильный номер и свободный текст допустимы
    assert "У-1" in cli("claim", "list", "--plan-question", "ВП-2")[1]
    assert cli("claim", "list", "--plan-question", "Сколько стоит подписка?")[0] == 0


# --- места использования: решения, пробелы, строки плана проверки ---


def test_usage_places_name_decisions_gaps_and_verification_rows(example, cli):
    code, out, err = cli("claim", "revoke", "У-3", "--reason", "неверно", "--project", "example")
    assert code == 0, err
    used = json.loads(cli("claim", "revoke", "У-3", "--reason", "x", "--json", "--project", "example")[1])
    assert used["already_revoked"] is True
    assert "решение Реш-1" in out  # У-3 — допущение решения
    assert "строка плана проверки ПП-1" in out  # гипотеза строки плана
    assert "пре-мортем, строка плана проверки ПП-4" in out
    code, out, err = cli("claim", "revoke", "У-5", "--reason", "неверно", "--project", "example")
    assert code == 0, err
    assert "пробел П-1" in out  # гипотеза пробела
    assert "строка плана проверки ПП-2" in out
    code, out, err = cli("claim", "revoke", "У-4", "--reason", "неверно", "--project", "example")
    assert code == 0, err
    assert "решение Реш-1" in out  # У-4 — основание решения
    assert "перечень показателей: «охват»" in out
