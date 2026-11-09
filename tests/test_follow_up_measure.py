"""Замер для ADR-0016: полнота и точность `due` и `impact` на проекте после выпуска с известной цепочкой.

Ожидаемые наборы записаны заранее, по данным фикстуры `tests/fixtures/project_follow_up/` (сроки, пороги,
основания, возражения), а не по ответам команд. Полнота — доля ожидаемого, которую назвала команда; точность —
доля названного, которое ожидалось. Лишнее и пропущенное считаются по каждому сценарию отдельно.
"""

from __future__ import annotations

from helpers import rehash
from test_follow_up import STALE_SOURCE, WHAT, call, due, fu, ids, keys, metric  # noqa: F401

ABSENT = frozenset()

# --- due: (сценарий, дата, значения показателей, ожидаемый набор) ---------------------------------
DUE_SCENARIOS = [
    ("всё впереди", "2026-08-01", [], ABSENT),
    ("источник И-3 состарился", "2026-08-29", [], {STALE_SOURCE}),
    (
        "обязательство вошло в окно",
        "2026-11-13",
        [],
        {STALE_SOURCE, ("commitments", "Реш-1", WHAT)},
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
    ),
    (
        "оба значения внесены на срок и не сработали",
        "2027-01-16",
        [("ПП-2", "10 %", "2027-01-15"), ("ПП-5", "40 %", "2027-01-15")],
        {("review", "Реш-1"), STALE_SOURCE, ("commitments", "Реш-1", WHAT)},
    ),
]

# --- impact: заранее записанные наборы по номерам -------------------------------------------------
ALL = ("claims", "plan_questions", "gaps", "options", "promises", "rows", "decisions")


def expect(**sets: list[str]) -> dict[str, list[str]]:
    return {k: sets.get(k, []) for k in ALL}


IMPACT_SCENARIOS = {
    "И-1": (
        expect(
            claims=["У-2", "У-3", "У-4", "У-5", "У-6"],
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
    "И-4": (expect(claims=["У-8"], options=["Вар-4"], decisions=["Реш-1"]), False),
    "У-2": (
        expect(
            claims=["У-3", "У-4", "У-5", "У-6"],
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
    "У-8": (expect(options=["Вар-4"], decisions=["Реш-1"]), False),
}


def score(got: set, want: set) -> tuple[int, int, int]:
    """(верно названо, лишнее, пропущено)."""
    return len(got & want), len(got - want), len(want - got)


def test_due_finds_every_expected_item_and_nothing_else(cli, fu):  # noqa: F811
    hit = extra = lost = 0
    for name, today, values, want in DUE_SCENARIOS:
        (fu / "metrics.yaml").unlink(missing_ok=True)  # каждый сценарий — с чистыми показателями
        rehash(fu)
        for row, value, date in values:
            assert metric(cli, row, value, date)[0] == 0, name
        got = keys(due(cli, today))
        h, e, m = score(got, set(want))
        assert (e, m) == (0, 0), f"{name}: лишнее {got - set(want)}, пропущено {set(want) - got}"
        hit, extra, lost = hit + h, extra + e, lost + m
    # числа для ADR-0016: ожидалось 16 пунктов в 6 сценариях
    assert (hit, extra, lost) == (16, 0, 0)


def test_impact_finds_every_expected_record_and_the_choice_flag(cli, fu):  # noqa: F811
    hit = extra = lost = 0
    for seed, (want, choice) in IMPACT_SCENARIOS.items():
        code, data, err = call(cli, "impact", seed)
        assert code == 0, err
        got = {(k, i) for k in ALL for i in ids(data[k])}
        expected = {(k, i) for k, v in want.items() for i in v}
        h, e, m = score(got, expected)
        assert (e, m) == (0, 0), f"{seed}: лишнее {got - expected}, пропущено {expected - got}"
        assert data["choice"]["affected"] is choice, seed
        hit, extra, lost = hit + h, extra + e, lost + m
    # числа для ADR-0016: ожидалось 66 записей в 10 сценариях; выбор на В4 угадан в 10 из 10
    assert (hit, extra, lost) == (66, 0, 0)
