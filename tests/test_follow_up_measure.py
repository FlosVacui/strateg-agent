"""Замер для ADR-0016: полнота и точность `due` и `impact` на проекте после выпуска с известной цепочкой.

Ожидаемые наборы записаны заранее, по данным фикстуры `tests/fixtures/project_follow_up/` (сроки, пороги,
основания, возражения, замены утверждений), а не по ответам команд. Полнота — доля ожидаемого, которую назвала
команда; точность — доля названного, которое ожидалось. Лишнее и пропущенное считаются по каждому сценарию.
"""

from __future__ import annotations

import shutil

from test_follow_up import (  # noqa: F401
    FIXTURE,
    STALE_SOURCE,
    WHAT,
    age_source,
    call,
    drop_u2_from_basis,
    due,
    fu,
    ids,
    keys,
    metric,
    quote_from_old_source,
    replace_decision_now,
    replace_u7_with_new_page,
)

ABSENT = frozenset()


def deep_revoked(cli, root):
    drop_u2_from_basis(root)
    assert cli("claim", "revoke", "У-2", "--reason", "страница изменилась", "--project", "fu")[0] == 0


def deep_old_source(cli, root):
    drop_u2_from_basis(root)
    age_source(root, "И-1")


def not_facts_on_old_source(cli, root):
    quote_from_old_source(root, "У-4")  # прогноз
    quote_from_old_source(root, "У-3")  # допущение


# --- due: (сценарий, дата, значения показателей, ожидаемый набор, подготовка проекта) -----------------------
DUE_SCENARIOS = [
    ("всё впереди", "2026-08-01", [], ABSENT, None),
    ("источник И-3 состарился", "2026-08-29", [], {STALE_SOURCE}, None),
    (
        "обязательство вошло в окно",
        "2026-11-13",
        [],
        {STALE_SOURCE, ("commitments", "Реш-1", WHAT)},
        None,
    ),
    (
        "срок пересмотра, сработавшее и просроченное условия",
        "2027-01-15",
        [("ПП-2", "35 %", "2027-01-10")],
        {
            ("review", "Реш-1"),
            ("check", "Реш-1", "ПП-5"),
            ("fired", "Реш-1", "ПП-2"),
            STALE_SOURCE,
            ("commitments", "Реш-1", WHAT),
        },
        None,
    ),
    (
        "условие на пороге не сработало, другое сработало ниже порога",
        "2027-01-15",
        [("ПП-2", "30 %", "2027-01-10"), ("ПП-5", "19 %", "2027-01-12")],
        {
            ("review", "Реш-1"),
            ("check", "Реш-1", "ПП-2"),  # значение до срока 2027-01-15: проверка за сроком не сделана
            ("fired", "Реш-1", "ПП-5"),
            STALE_SOURCE,
            ("commitments", "Реш-1", WHAT),
        },
        None,
    ),
    (
        "оба значения внесены на срок и не сработали",
        "2027-01-16",
        [("ПП-2", "10 %", "2027-01-15"), ("ПП-5", "40 %", "2027-01-15")],
        {("review", "Реш-1"), STALE_SOURCE, ("commitments", "Реш-1", WHAT)},
        None,
    ),
    (
        "значение от дня после сегодняшнего не засчитано",
        "2027-01-15",
        [("ПП-2", "35 %", "2027-01-20")],
        {
            ("review", "Реш-1"),
            ("check", "Реш-1", "ПП-5"),
            ("check", "Реш-1", "ПП-2"),
            STALE_SOURCE,
            ("commitments", "Реш-1", WHAT),
        },
        None,
    ),
    (
        "два значения на одну дату: решает записанное позже",
        "2027-01-16",
        [("ПП-2", "35 %", "2027-01-15"), ("ПП-2", "10 %", "2027-01-15"), ("ПП-5", "40 %", "2027-01-15")],
        {("review", "Реш-1"), STALE_SOURCE, ("commitments", "Реш-1", WHAT)},
        None,
    ),
    (
        "основание отозвано глубже первого шага",
        "2026-07-01",
        [],
        {("stale", "Реш-1", "У-2", "отозвано")},
        deep_revoked,
    ),
    (
        "факт в глубине держится на старом источнике",
        "2026-08-01",
        [],
        {("stale", "Реш-1", "У-2", "источник устарел")},
        deep_old_source,
    ),
    (
        "прогноз и допущение на старом источнике не устаревают",
        "2026-09-01",
        [],
        {STALE_SOURCE},
        not_facts_on_old_source,
    ),
    (
        "значение до решения не срабатывает по условию нового решения",
        "2027-06-02",
        [("ПП-2", "35 %", "2027-01-20")],
        {
            ("review", "Реш-2"),
            ("check", "Реш-2", "ПП-2"),
            ("check", "Реш-2", "ПП-5"),
            ("stale", "Реш-2", "У-7", "источник устарел"),
            ("stale", "Реш-2", "У-2", "источник устарел"),
        },
        replace_decision_now,
    ),
]

# --- impact: заранее записанные наборы по номерам -----------------------------------------------------------
ALL = ("claims", "plan_questions", "gaps", "options", "promises", "rows", "decisions")


def expect(**sets: list[str]) -> dict[str, list[str]]:
    return {k: sets.get(k, []) for k in ALL}


IMPACT_SCENARIOS = {
    "И-1": (
        expect(
            claims=["У-1", "У-2", "У-3", "У-4", "У-5", "У-6"],  # У-1 заменено на У-2
            plan_questions=["ВП-1", "ВП-2"],
            gaps=["П-1"],
            options=["Вар-1", "Вар-2", "Вар-3", "Вар-4"],
            promises=["О-1"],
            rows=["ПП-1", "ПП-2", "ПП-3", "ПП-4"],
            decisions=["Реш-1"],
        ),
        True,
    ),
    "И-3": (
        expect(
            claims=["У-7"], plan_questions=["ВП-1"], options=["Вар-3"], promises=["О-3"], decisions=["Реш-1"]
        ),
        True,
    ),
    "И-4": (expect(claims=["У-8", "У-9"], options=["Вар-4"], decisions=["Реш-1"]), False),
    "И-5": (expect(claims=["У-8", "У-9"], options=["Вар-4"], decisions=["Реш-1"]), False),
    "У-2": (
        expect(
            claims=["У-1", "У-3", "У-4", "У-5", "У-6"],
            plan_questions=["ВП-1", "ВП-2"],
            gaps=["П-1"],
            options=["Вар-1", "Вар-2", "Вар-3", "Вар-4"],
            promises=["О-1"],
            rows=["ПП-1", "ПП-2", "ПП-3", "ПП-4"],
            decisions=["Реш-1"],
        ),
        True,
    ),
    "У-3": (
        expect(
            claims=["У-6"],
            plan_questions=["ВП-2"],
            options=["Вар-1", "Вар-2", "Вар-3"],
            rows=["ПП-1", "ПП-3", "ПП-4"],
            decisions=["Реш-1"],
        ),
        True,
    ),
    "У-4": (expect(options=["Вар-2"], decisions=["Реш-1"]), True),
    "У-5": (expect(gaps=["П-1"], options=["Вар-2"], rows=["ПП-1", "ПП-2"], decisions=["Реш-1"]), True),
    "У-6": (expect(rows=["ПП-3"]), False),
    "У-7": (expect(plan_questions=["ВП-1"], options=["Вар-3"], promises=["О-3"], decisions=["Реш-1"]), True),
    "У-8": (expect(claims=["У-9"], options=["Вар-4"], decisions=["Реш-1"]), False),
    "У-9": (expect(claims=["У-8"], options=["Вар-4"], decisions=["Реш-1"]), False),
}

# Новая страница заменяет У-7: на нём стоят возражение Вар-3, обещание О-3 и основание решения Реш-1.
AFTER_U7_REPLACED = {
    "source": expect(
        claims=["У-7", "У-10"],
        plan_questions=["ВП-1"],
        options=["Вар-3"],
        promises=["О-3"],
        decisions=["Реш-1"],
    ),
    "new": expect(
        claims=["У-7"], plan_questions=["ВП-1"], options=["Вар-3"], promises=["О-3"], decisions=["Реш-1"]
    ),
    "old": expect(
        claims=["У-10"], plan_questions=["ВП-1"], options=["Вар-3"], promises=["О-3"], decisions=["Реш-1"]
    ),
}


def score(got: set, want: set) -> tuple[int, int, int]:
    """(верно названо, лишнее, пропущено)."""
    return len(got & want), len(got - want), len(want - got)


def test_due_finds_every_expected_item_and_nothing_else(cli, fu):  # noqa: F811
    hit = extra = lost = 0
    for name, today, values, want, prep in DUE_SCENARIOS:
        shutil.rmtree(fu)  # каждый сценарий — со свежей копией фикстуры
        shutil.copytree(FIXTURE, fu)
        if prep is not None:
            prep(cli, fu)
        for row, value, date in values:
            assert metric(cli, row, value, date)[0] == 0, name
        got = keys(due(cli, today))
        h, e, m = score(got, set(want))
        assert (e, m) == (0, 0), f"{name}: лишнее {got - set(want)}, пропущено {set(want) - got}"
        hit, extra, lost = hit + h, extra + e, lost + m
    # числа для ADR-0016: ожидалось 32 пункта в 12 сценариях
    assert (hit, extra, lost) == (32, 0, 0)


def check_impact(cli, seed: str, want: dict[str, list[str]], choice: bool) -> tuple[int, int, int]:
    code, data, err = call(cli, "impact", seed)
    assert code == 0, err
    got = {(k, i) for k in ALL for i in ids(data[k])}
    expected = {(k, i) for k, v in want.items() for i in v}
    assert data["choice"]["affected"] is choice, seed
    h, e, m = score(got, expected)
    assert (e, m) == (0, 0), f"{seed}: лишнее {got - expected}, пропущено {expected - got}"
    return h, e, m


def test_impact_finds_every_expected_record_and_the_choice_flag(cli, fu, tmp_path):  # noqa: F811
    hit = extra = lost = scenarios = 0
    for seed, (want, choice) in IMPACT_SCENARIOS.items():
        h, e, m = check_impact(cli, seed, want, choice)
        hit, extra, lost, scenarios = hit + h, extra + e, lost + m, scenarios + 1
    source, new = replace_u7_with_new_page(cli, fu, tmp_path)  # источник И-6, утверждение У-10
    assert (source, new) == ("И-6", "У-10")
    for seed, key in ((source, "source"), (new, "new"), ("У-7", "old")):
        h, e, m = check_impact(cli, seed, AFTER_U7_REPLACED[key], True)
        hit, extra, lost, scenarios = hit + h, extra + e, lost + m, scenarios + 1
    # числа для ADR-0016: ожидалось 93 записи в 15 сценариях; выбор на В4 угадан в 15 из 15
    assert scenarios == 15
    assert (hit, extra, lost) == (93, 0, 0)
