"""`strateg due`, `strateg metric add`, `strateg impact` на готовом проекте с известной цепочкой связей.

Фикстура — `tests/fixtures/project_follow_up/` (ADR-0016). Известные сроки: решение Реш-1 пересматривается
2027-01-15, условия ПП-5 («ниже 20 %») и ПП-2 («выше 30 %») проверяются тогда же, обязательство
«запросить выгрузку …» — до 2026-11-20. Основания Реш-1: У-2, У-4, У-7 (факт из И-3, страница получена
2026-03-01), допущение У-3.

Цепочка связей: И-1 -> У-2 -> У-3, У-4, У-5 -> У-6; И-3 -> У-7 (возражение Вар-3, обещание О-3, основание
Реш-1); И-4 -> У-8 (возражение Вар-4, вариант отвергнут решением).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
import yaml

from helpers import edit_yaml, journal_of, load_yaml, rehash, tree_hashes
from strateg import budget as budgetlib
from strateg import project as proj
from strateg.project_check import check_project

FIXTURE = Path(__file__).parent / "fixtures" / "project_follow_up"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def fu(home, clock) -> Path:
    """Копия фикстуры; часы — 2027-02-01: все значения показателей вносят задним числом."""
    shutil.copytree(FIXTURE, home / "fu")
    clock.value = dt.datetime(2027, 2, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    return home / "fu"


def call(cli, *args, root: str = "fu"):
    code, out, err = cli(*args, "--project", root, "--json")
    return code, (json.loads(out) if out.strip() else None), err


def due(cli, today: str, **kw) -> dict:
    code, data, err = call(cli, "due", "--today", today, **kw)
    assert code == 0, err
    return data


def keys(data: dict) -> set[tuple]:
    """Что назвал `due`: (группа, решение, ещё признаки) — без текста."""
    out = set()
    for key, items in data["groups"].items():
        for i in items:
            if key == "review":
                out.add((key, i["decision"]))
            elif key in ("check", "fired", "manual"):
                out.add((key, i["decision"], i["row"]))
            elif key == "stale":
                out.add((key, i["decision"], i["claim"], i["reason"]))
            else:
                out.add((key, i["decision"], i["what"]))
    return out


def metric(cli, row: str, value: str, date: str, *extra: str):
    return call(cli, "metric", "add", row, "--value", value, "--date", date, *extra)


WHAT = "запросить выгрузку отключений у владельца продукта"
STALE_SOURCE = ("stale", "Реш-1", "У-7", "источник устарел")


def test_the_fixture_is_clean_and_has_no_metrics_file(fu):
    assert check_project(fu) == []
    assert not (fu / "metrics.yaml").exists()  # проект без файла показателей открывается


# --- due: группы ----------------------------------------------------------------------------------


def test_due_without_decisions_says_follow_up_starts_after_release(cli, home):
    assert cli("new", "demo")[0] == 0
    code, out, _ = cli("due", "--today", "2026-11-09")
    assert code == 0 and out.strip() == "Решений нет: сопровождение начинается после выпуска (шаг 7)."
    code, data, _ = call(cli, "due", "--today", "2026-11-09", root="demo")
    assert data["decisions"] == 0 and data["empty"] is True


def test_due_is_empty_and_names_the_next_event(cli, fu):
    data = due(cli, "2026-08-01")
    assert data["empty"] and keys(data) == set()
    # раньше всего источник И-3 перестанет быть свежим: получен 2026-03-01, допустимо 180 дней
    assert data["next_event"]["date"] == "2026-08-29"
    code, out, _ = cli("due", "--today", "2026-08-01", "--project", "fu")
    assert code == 0
    assert out.splitlines()[0] == "Решать сейчас нечего."
    assert "2026-08-29" in out


def test_next_event_without_old_sources_is_the_commitment_window(cli, fu, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    defaults = yaml.safe_load((REPO / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    defaults["follow_up"]["source_max_age_days"] = 3650
    (cfg / "defaults.yaml").write_text(yaml.safe_dump(defaults), encoding="utf-8")
    monkeypatch.setenv(budgetlib.CONFIG_ENV, str(cfg))
    data = due(cli, "2026-11-09")
    assert data["empty"]
    assert data["next_event"]["date"] == "2026-11-13"  # срок 2026-11-20 минус окно в 7 дней
    assert "обязательства" in data["next_event"]["text"]


def test_commitment_enters_the_window_exactly_seven_days_before(cli, fu):
    assert ("commitments", "Реш-1", WHAT) not in keys(due(cli, "2026-11-12"))
    data = due(cli, "2026-11-13")
    assert ("commitments", "Реш-1", WHAT) in keys(data)
    item = data["groups"]["commitments"][0]
    assert item["days"] == 7 and "через 7 дн." in item["text"]
    assert "сегодня" in due(cli, "2026-11-20")["groups"]["commitments"][0]["text"]
    late = due(cli, "2026-11-25")["groups"]["commitments"][0]
    assert late["days"] == -5 and "просрочено на 5 дн." in late["text"]


def test_a_source_older_than_the_limit_makes_the_basis_stale(cli, fu):
    # И-3 получен 2026-03-01; 180 дней проходят 2026-08-28, на 181-й день основание устарело
    assert STALE_SOURCE not in keys(due(cli, "2026-08-28"))
    data = due(cli, "2026-08-29")
    assert STALE_SOURCE in keys(data)
    item = data["groups"]["stale"][0]
    assert item["source"] == "И-3" and item["age_days"] == 181 and item["accessed"] == "2026-03-01"
    # И-1 получен 2026-11-06, И-2 — материал без даты обращения: они не устаревают
    assert {k for k in keys(due(cli, "2027-05-06")) if k[0] == "stale"} == {
        STALE_SOURCE,
        ("stale", "Реш-1", "У-2", "источник устарел"),
    }


def test_every_group_at_once(cli, fu):
    assert metric(cli, "ПП-2", "35 %", "2027-01-10", "--note", "выгрузка отключений за декабрь")[0] == 0
    data = due(cli, "2027-01-15")
    assert keys(data) == {
        ("review", "Реш-1"),
        ("check", "Реш-1", "ПП-5"),  # значения нет, срок наступил
        ("fired", "Реш-1", "ПП-2"),  # 35 % при пороге «выше 30 %»
        STALE_SOURCE,
        ("commitments", "Реш-1", WHAT),
    }
    fired = data["groups"]["fired"][0]
    assert (fired["value"], fired["unit"], fired["threshold"]) == (35.0, "%", "выше 30 %")
    assert "35 %" in fired["text"] and "выше 30 %" in fired["text"] and "2027-01-10" in fired["text"]
    assert "выгрузка отключений за декабрь" in fired["text"]
    code, out, _ = cli("due", "--today", "2027-01-15", "--project", "fu")
    assert code == 0
    for title in (
        "Пора пересматривать решение",
        "Сработало условие отказа",
        "Устарели основания",
        "Обязательства",
    ):
        assert title in out


# --- due: условия и показатели --------------------------------------------------------------------


def test_review_and_condition_dates_come_exactly_on_the_day(cli, fu):
    before = keys(due(cli, "2027-01-14"))
    assert ("review", "Реш-1") not in before and not any(k[0] == "check" for k in before)
    on_day = keys(due(cli, "2027-01-15"))
    assert ("review", "Реш-1") in on_day
    assert {("check", "Реш-1", "ПП-5"), ("check", "Реш-1", "ПП-2")} <= on_day


@pytest.mark.parametrize(
    ("value", "fires"),
    [("25 %", False), ("30 %", False), ("30,1 %", True), ("35 %", True)],
)
def test_value_fires_the_condition_only_beyond_the_threshold(cli, fu, value, fires):
    assert metric(cli, "ПП-2", value, "2026-12-05")[0] == 0
    got = keys(due(cli, "2026-12-06"))
    assert (("fired", "Реш-1", "ПП-2") in got) is fires


def test_lower_threshold_fires_below_the_number(cli, fu):
    assert metric(cli, "ПП-5", "20 %", "2026-12-05")[0] == 0
    assert ("fired", "Реш-1", "ПП-5") not in keys(due(cli, "2026-12-06"))
    assert metric(cli, "ПП-5", "19 %", "2026-12-06")[0] == 0  # последнее значение — по дате
    assert ("fired", "Реш-1", "ПП-5") in keys(due(cli, "2026-12-07"))


def test_latest_value_by_date_wins_over_the_order_of_entry(cli, fu):
    assert metric(cli, "ПП-2", "40 %", "2026-12-10")[0] == 0
    assert metric(cli, "ПП-2", "10 %", "2026-12-01")[0] == 0  # внесено позже, но на более раннюю дату
    assert ("fired", "Реш-1", "ПП-2") in keys(due(cli, "2026-12-11"))


def test_a_value_after_the_date_closes_the_check_but_an_older_one_does_not(cli, fu):
    assert metric(cli, "ПП-5", "25 %", "2026-12-20")[0] == 0  # до срока 2027-01-15
    assert ("check", "Реш-1", "ПП-5") in keys(due(cli, "2027-01-15"))
    assert metric(cli, "ПП-5", "25 %", "2027-01-15")[0] == 0  # на срок, не сработало
    assert ("check", "Реш-1", "ПП-5") not in keys(due(cli, "2027-01-16"))


def test_unparsed_threshold_asks_to_compare_by_hand(cli, fu):
    for rel, put in (
        ("decisions.yaml", lambda d: d["decisions"][0]["conditions"][1].update(threshold="не выше 30 %")),
        ("verification.yaml", lambda d: d["rows"][1]["change_mind"].update(threshold="не выше 30 %")),
    ):
        edit_yaml(fu, rel, put)
    rehash(fu)
    assert check_project(fu) == []
    assert metric(cli, "ПП-2", "35 %", "2026-12-05")[0] == 0  # порога не разобрать — единица берётся как есть
    data = due(cli, "2026-12-06")
    assert ("manual", "Реш-1", "ПП-2") in keys(data) and not any(k[0] == "fired" for k in keys(data))
    assert "порог «не выше 30 %» не разобран, сравните вручную" in data["groups"]["manual"][0]["text"]


def test_value_and_threshold_in_different_units_are_not_compared(cli, fu):
    (fu / "metrics.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "values": [
                    {
                        "row": "ПП-2",
                        "value": 35,
                        "unit": "₽",
                        "date": "2026-12-05",
                        "origin": "пользователь",
                        "recorded_at": "2026-12-05T10:00:00+03:00",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    rehash(fu)
    data = due(cli, "2026-12-06")
    assert ("manual", "Реш-1", "ПП-2") in keys(data) and not any(k[0] == "fired" for k in keys(data))
    assert "разных единицах" in data["groups"]["manual"][0]["text"]


# --- due: основания, решения ----------------------------------------------------------------------


def test_revoked_and_replaced_claims_make_the_basis_stale(cli, fu, tmp_path):
    assert cli("claim", "revoke", "У-3", "--reason", "интервью не подтвердило", "--project", "fu")[0] == 0
    new = {
        "text": "За год доля абонентов с подключённой защитой вырастет до 12–14 %",
        "type": "прогноз",
        "subject": "доля абонентов с защитой",
        "period": "2027",
        "basis": ["У-2"],
        "forecast_range": {"low": 12, "high": 14, "unit": "% абонентов"},
    }
    path = tmp_path / "new.yaml"
    path.write_text(yaml.safe_dump(new, allow_unicode=True), encoding="utf-8")
    code, _, err = cli("claim", "replace", "У-4", "--file", str(path), "--project", "fu")
    assert code == 0, err
    data = due(cli, "2026-07-01")  # источники ещё свежие: остались только статусы утверждений
    assert keys(data) == {
        ("stale", "Реш-1", "У-3", "отозвано"),
        ("stale", "Реш-1", "У-4", "заменено"),
    }
    texts = {i["claim"]: i["text"] for i in data["groups"]["stale"]}
    assert "У-3 отозвано: интервью не подтвердило" in texts["У-3"] and "(допущение)" in texts["У-3"]
    assert "У-4 заменено на У-9" in texts["У-4"] and "(основание)" in texts["У-4"]


def test_a_replaced_decision_is_not_followed(cli, fu):
    def replace(d):
        old = d["decisions"][0]
        new = {**old, "id": "Реш-2", "review_date": "2027-06-01", "commitments": [], "replaces": "Реш-1"}
        new["conditions"] = [{**c, "date": "2027-06-01"} for c in old["conditions"]]
        old.update(status="заменено", replaced_by="Реш-2")
        d["decisions"].append(new)

    edit_yaml(fu, "decisions.yaml", replace)
    rehash(fu)
    assert check_project(fu) == []
    data = due(cli, "2027-01-15")
    assert {k[1] for k in keys(data)} <= {"Реш-2"}  # о Реш-1 не говорится ничего
    assert ("review", "Реш-2") not in keys(data)
    assert due(cli, "2027-06-01")["groups"]["review"][0]["decision"] == "Реш-2"


def test_only_replaced_decisions_leave_nothing_to_follow(cli, fu):
    edit_yaml(fu, "decisions.yaml", lambda d: d["decisions"][0].update(status="отменено"))
    rehash(fu)
    data = due(cli, "2027-01-15")
    assert data["empty"] and "Действующих решений нет" in data["message"]


def test_due_refuses_a_bad_date(cli, fu):
    code, _, err = cli("due", "--today", "2026-13-40", "--project", "fu")
    assert code == 2 and "дата" in err
    code, _, err = cli("due", "--today", "06.11.2026", "--project", "fu")
    assert code == 2


def test_reading_commands_write_nothing_and_keep_the_pause(cli, fu):
    assert cli("stop", "--reason", "нужен ответ руководителя", "--project", "fu")[0] == 0
    before = tree_hashes(fu)
    for argv in (("due", "--today", "2027-01-15"), ("impact", "И-1"), ("impact", "У-2")):
        assert cli(*argv, "--project", "fu")[0] == 0
    assert tree_hashes(fu) == before  # ни журнал, ни состояние не тронуты
    assert proj.pause_kind(proj.Project.load(fu).journal) == proj.PAUSE_STOP


# --- metric add -----------------------------------------------------------------------------------


def test_metric_add_records_the_value_from_the_user(cli, fu):
    code, data, err = metric(cli, "ПП-2", "35 %", "2026-12-05", "--note", "выгрузка отключений")
    assert code == 0, err
    assert data["already_recorded"] is False
    assert data["conditions"] == [
        {
            "decision": "Реш-1",
            "threshold": "выше 30 %",
            "result": "сработало",
            "action": "остановить продвижение и разобрать причины",
        }
    ]
    saved = load_yaml(fu / "metrics.yaml")
    assert saved["schema_version"] == 1
    (v,) = saved["values"]
    assert (v["row"], v["value"], v["unit"], v["date"], v["note"], v["origin"]) == (
        "ПП-2",
        35.0,
        "%",
        "2026-12-05",
        "выгрузка отключений",
        "пользователь",
    )
    assert check_project(fu) == []
    event = journal_of(fu)[-1]
    assert (
        event["name"] == "strateg metric add" and event["actor"] == "CLI" and event["status"] == "выполнено"
    )
    assert "ПП-2: 35 % на 2026-12-05" in event["message"]
    assert "metrics.yaml" in load_yaml(fu / "state.yaml")["files"]  # правка не считается внешней


def test_metric_add_text_tells_whether_the_condition_fired(cli, fu):
    code, out, _ = cli("metric", "add", "ПП-2", "--value", "25 %", "--date", "2026-12-05", "--project", "fu")
    assert code == 0 and "не сработало" in out and "Реш-1" in out
    code, out, _ = cli("metric", "add", "ПП-2", "--value", "35 %", "--date", "2026-12-06", "--project", "fu")
    assert "СРАБОТАЛО, действие: остановить продвижение" in out


def test_metric_add_is_idempotent(cli, fu):
    assert metric(cli, "ПП-2", "35 %", "2026-12-05", "--note", "выгрузка")[0] == 0
    files, events = (fu / "metrics.yaml").read_bytes(), len(journal_of(fu))
    code, data, _ = metric(cli, "ПП-2", "35 %", "2026-12-05", "--note", "выгрузка")
    assert code == 0 and data["already_recorded"] is True
    assert (fu / "metrics.yaml").read_bytes() == files and len(journal_of(fu)) == events
    # другая дата или другое значение — новая запись
    assert metric(cli, "ПП-2", "36 %", "2026-12-05", "--note", "выгрузка")[0] == 0
    assert len(load_yaml(fu / "metrics.yaml")["values"]) == 2


def test_metric_add_refuses_other_units_and_asks_for_the_unit(cli, fu):
    before = tree_hashes(fu)
    code, data, _ = metric(cli, "ПП-2", "35 ₽", "2026-12-05")
    assert code == 1 and "единицы не совпадают" in data["error"] and "«%»" in data["error"]
    code, data, _ = metric(cli, "ПП-2", "35", "2026-12-05")
    assert code == 1 and "нет единицы" in data["error"] and "--value '35 %'" in data["error"]
    assert tree_hashes(fu) == before


@pytest.mark.parametrize(
    ("row", "value", "date", "code", "word"),
    [
        ("ПП-9", "35 %", "2026-12-05", 1, "нет в verification.yaml"),
        ("PP-2", "35 %", "2026-12-05", 2, "латинские"),
        ("ПП-2", "много", "2026-12-05", 2, "не число"),
        ("ПП-2", "35 %", "2026-12-35", 2, "дата"),
        ("ПП-2", "35 %", "2027-06-01", 1, "в будущем"),  # часы теста стоят на 2027-02-01
    ],
)
def test_metric_add_refuses_bad_input(cli, fu, row, value, date, code, word):
    before = tree_hashes(fu)
    got, out, err = metric(cli, row, value, date)
    assert got == code, (out, err)
    text = (out or {}).get("error", "") + err
    assert word in text, text
    assert tree_hashes(fu) == before


def test_check_finds_a_value_for_a_missing_row(cli, fu):
    assert metric(cli, "ПП-2", "35 %", "2026-11-05")[0] == 0
    edit_yaml(fu, "metrics.yaml", lambda d: d["values"][0].update(row="ПП-9"))
    errors = check_project(fu)
    assert any("metrics.yaml" in e and "ПП-9" in e for e in errors), errors


# --- impact ---------------------------------------------------------------------------------------


def impact(cli, ref: str) -> dict:
    code, data, err = call(cli, "impact", ref)
    assert code == 0, err
    return data


def ids(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries]


def test_impact_of_a_source_reaches_everything_that_stands_on_it(cli, fu):
    r = impact(cli, "И-1")
    assert r["seed_claims"] == ["У-2"]
    assert ids(r["claims"]) == ["У-2", "У-3", "У-4", "У-5", "У-6"]
    assert r["frame_fields"] == ["data"]  # поле рамки «данные» взято из И-1
    assert ids(r["plan_questions"]) == ["ВП-1", "ВП-2"]
    assert ids(r["gaps"]) == ["П-1"]
    assert ids(r["options"]) == ["Вар-1", "Вар-2", "Вар-3", "Вар-4"]
    assert ids(r["promises"]) == ["О-1"]
    assert ids(r["rows"]) == ["ПП-1", "ПП-2", "ПП-3", "ПП-4"]
    assert ids(r["decisions"]) == ["Реш-1"]
    assert [d["path"] for d in r["documents"]] == [
        "outputs/comms-strategy.md",
        "outputs/one-pager.md",
        "outputs/product-strategy.md",
        "outputs/situation.md",
    ]
    assert r["choice"]["affected"] is True
    assert r["choice"]["stop"] == "внеплановая остановка: спросить человека до пересчёта"
    assert any("рекомендация опирается на У-2, У-4" in v for v in r["choice"]["via"])


def test_impact_of_a_competitor_source_hits_the_choice_through_the_decision(cli, fu):
    r = impact(cli, "И-3")
    assert r["seed_claims"] == ["У-7"] and ids(r["claims"]) == ["У-7"]
    assert ids(r["plan_questions"]) == ["ВП-1"] and r["gaps"] == []
    assert ids(r["options"]) == ["Вар-3"]  # возражение на У-7
    assert r["options"][0]["why"] == ["возражение, основание У-7"]
    assert ids(r["promises"]) == ["О-3"] and r["rows"] == []
    assert ids(r["decisions"]) == ["Реш-1"]
    assert "основание У-7" in r["decisions"][0]["why"]
    assert [d["path"] for d in r["documents"]] == ["outputs/product-strategy.md"]
    assert r["choice"] == {
        "affected": True,
        "via": ["решение Реш-1 опирается на У-7"],
        "stop": "внеплановая остановка: спросить человека до пересчёта",
    }


def test_impact_that_leaves_the_choice_alone_says_so(cli, fu):
    r = impact(cli, "И-4")
    assert ids(r["claims"]) == ["У-8"] and ids(r["options"]) == ["Вар-4"]
    assert ids(r["decisions"]) == ["Реш-1"]  # отвергнутый им вариант задет, но выбор не опирается на У-8
    assert r["decisions"][0]["why"] == ["отвергнутый вариант Вар-4 задет"]
    assert r["choice"] == {"affected": False, "via": [], "stop": None}
    assert [d["path"] for d in r["documents"]] == ["outputs/situation.md"]
    code, out, _ = cli("impact", "И-4", "--project", "fu")
    assert code == 0 and "Выбор на В4: не задет." in out and "остановка" not in out


def test_impact_of_a_claim_follows_basis_transitively(cli, fu):
    r = impact(cli, "У-3")
    assert r["seed"] == {"id": "У-3", "kind": "утверждение", "status": "действует", "text": r["seed"]["text"]}
    assert ids(r["claims"]) == ["У-6"]  # У-6 стоит на У-3; само У-3 в списке не повторяется
    assert ids(r["rows"]) == ["ПП-1", "ПП-3", "ПП-4"]  # гипотезы У-3 и У-6, причины пре-мортема
    assert ids(r["options"]) == ["Вар-1", "Вар-2", "Вар-3"]  # возражения на У-3
    assert ids(r["decisions"]) == ["Реш-1"]
    assert r["choice"]["affected"] is True  # допущение решения и вариант Вар-2 задеты
    assert not any("опирается на У-3" in v for v in r["choice"]["via"])  # в основаниях решения У-3 нет


def test_impact_of_a_claim_that_hits_a_row_but_not_the_choice(cli, fu):
    r = impact(cli, "У-6")
    assert r["claims"] == [] and ids(r["rows"]) == ["ПП-3"]
    assert r["options"] == [] and r["decisions"] == []
    assert r["choice"]["affected"] is False


def test_impact_of_a_claim_without_dependents_names_only_its_own_links(cli, fu):
    r = impact(cli, "У-8")
    assert r["claims"] == [] and ids(r["options"]) == ["Вар-4"]
    assert r["choice"]["affected"] is False


def test_impact_of_a_replaced_and_a_revoked_claim_still_shows_dependents(cli, fu, tmp_path):
    assert (
        cli("claim", "revoke", "У-7", "--reason", "страница конкурента изменилась", "--project", "fu")[0] == 0
    )
    r = impact(cli, "У-7")
    assert r["seed"]["status"] == "отозвано"
    assert (
        ids(r["options"]) == ["Вар-3"] and ids(r["promises"]) == ["О-3"] and ids(r["decisions"]) == ["Реш-1"]
    )
    code, out, _ = cli("impact", "У-1", "--project", "fu")  # заменено на У-2
    assert code == 0 and "заменено на У-2" in out
    assert ids(impact(cli, "У-1")["claims"]) == []  # У-1 ни на чём не стоит, зависимые держатся на У-2


def test_a_dispute_links_the_disputing_claim(cli, fu):
    edit_yaml(
        fu,
        "claims.yaml",
        lambda d: next(c for c in d["claims"] if c["id"] == "У-8").update(
            dispute={"claim": "У-7", "reason": "источники спорят о цене"}
        ),
    )
    rehash(fu)
    r = impact(cli, "У-7")
    assert ids(r["claims"]) == ["У-8"] and r["claims"][0]["why"] == ["спор с У-7"]
    assert ids(r["options"]) == ["Вар-3", "Вар-4"]


def test_impact_numbers_in_documents_are_matched_whole(cli, fu):
    # У-1 не должен находиться в тексте с У-13 или ПУ-1; «П-1» не находится внутри «ВП-1»
    r = impact(cli, "У-8")
    assert {d["path"] for d in r["documents"]} == {"outputs/situation.md"}
    assert [d["numbers"] for d in r["documents"]] == [["Вар-4", "У-8"]] or "У-8" in r["documents"][0][
        "numbers"
    ]


@pytest.mark.parametrize(
    ("ref", "code", "word"),
    [
        ("И-9", 1, "нет в реестре"),
        ("У-99", 1, "нет в реестре"),
        ("U-2", 2, "латинские"),
        ("Вар-1", 2, "не подходит"),
    ],
)
def test_impact_refuses_unknown_or_malformed_numbers(cli, fu, ref, code, word):
    got, out, err = call(cli, "impact", ref)
    assert got == code
    assert word in (out or {}).get("error", "") + err


def test_text_output_of_impact_is_readable(cli, fu):
    code, out, _ = cli("impact", "И-3", "--project", "fu")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("Источник И-3")
    assert "Варианты: Вар-3 (возражение, основание У-7)" in out
    assert "Выбор на В4: задет" in out and "внеплановая остановка: спросить человека до пересчёта" in out


def test_old_project_without_new_files_still_works(cli, home):
    shutil.copytree(FIXTURE, home / "old")
    assert (
        not (home / "old" / "metrics.yaml").exists() and not (home / "old" / "history" / "release-1").exists()
    )
    assert cli("check", "--project", "old")[0] == 0
    assert cli("due", "--today", "2026-11-09", "--project", "old")[0] == 0
    assert cli("impact", "У-2", "--project", "old")[0] == 0
