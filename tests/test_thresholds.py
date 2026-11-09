"""Порог и значение показателя: консервативный разбор по числу, знаку и единице (ADR-0016)."""

from __future__ import annotations

import pytest

from strateg import thresholds as th


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("больше 15 %", (">", 15.0, "%")),
        ("выше 30 %", (">", 30.0, "%")),
        ("ниже 33 %", ("<", 33.0, "%")),
        ("Ниже 20%", ("<", 20.0, "%")),
        ("меньше 0 ₽", ("<", 0.0, "₽")),
        ("меньше -5 %", ("<", -5.0, "%")),
        ("меньше −5 %", ("<", -5.0, "%")),
        ("больше 15 000 ₽", (">", 15000.0, "₽")),
        ("больше 12,5 процента", (">", 12.5, "%")),
        ("ниже 300 рублей", ("<", 300.0, "₽")),
        ("выше 30 % подключивших", (">", 30.0, "%")),  # пояснение без цифр после единицы допустимо
        ("выше 30", (">", 30.0, None)),
        ("> 4", (">", 4.0, None)),
        ("ниже 20 %.", ("<", 20.0, "%")),
    ],
)
def test_threshold_is_parsed(text, want):
    t = th.parse_threshold(text)
    assert t is not None, text
    assert (t.op, t.number, t.unit) == want


@pytest.mark.parametrize(
    "text",
    [
        "не ниже 20 %",  # что считать срабатыванием, неясно
        "не больше 15 %",
        "около 15 %",
        "в 2 раза",
        "ниже 20 % или выше 40 %",
        "ниже 20 % и выше 40 %",
        "ниже 20 % за 3 месяца",
        "больше 20 обращений на 1000",
        "больше 20 000 подписчиков",
        "больше половины",
        "ниже 20 п. п.",
        "падение ниже 20 %",
        "",
    ],
)
def test_unclear_threshold_is_not_parsed(text):
    assert th.parse_threshold(text) is None, text


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("18", (18.0, None)),
        ("18 %", (18.0, "%")),
        ("18%", (18.0, "%")),
        ("18,5 %", (18.5, "%")),
        ("1 500 ₽", (1500.0, "₽")),
        ("1500 руб.", (1500.0, "₽")),
        ("-3 %", (-3.0, "%")),
    ],
)
def test_value_is_parsed(text, want):
    assert th.parse_value(text) == want


@pytest.mark.parametrize("text", ["", "много", "18 штук", "18 % подключивших", "1e3", "18 20"])
def test_value_that_is_not_a_number_is_refused(text):
    assert th.parse_value(text) is None


def test_threshold_fires_strictly_by_sign():
    above = th.parse_threshold("больше 15 %")
    below = th.parse_threshold("ниже 33 %")
    assert above and below
    assert th.fires(above, 15.1) and not th.fires(above, 15) and not th.fires(above, 14)
    assert th.fires(below, 32.9) and not th.fires(below, 33) and not th.fires(below, 40)
    negative = th.parse_threshold("меньше 0 ₽")
    assert negative and th.fires(negative, -1) and not th.fires(negative, 0)


def test_numbers_are_shown_in_russian_style():
    assert th.show_number(15.0) == "15"
    assert th.show_number(12.5) == "12,5"
    assert th.show_number(15000.0) == "15 000"
    assert th.show_amount(18.0, "%") == "18 %"
    assert th.show_amount(18.0, None) == "18"
