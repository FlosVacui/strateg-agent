"""Замены номеров: время, штрихкоды, СНИЛС и карты на страницах (ADR-0011)."""

from __future__ import annotations

import random
import time

import pytest

from safety_measure import ean13, luhn_number, snils
from strateg import safety

# --- время: число замен не должно давать квадрат --------------------------------------------------


@pytest.mark.parametrize(
    ("chunk", "label"),
    [
        ("a@b.ru ", "[почта]"),
        ("+7 999 123-45-67 ", "[телефон]"),
        ("8-800-555-35-35 +7 999 123-45-67 ", "[телефон]"),
    ],
)
def test_mask_personal_time_is_near_linear_for_forty_thousand_replacements(chunk, label):
    text = chunk * 40_000
    started = time.perf_counter()
    masked, replacements, _ = safety.mask_personal(text)
    elapsed = time.perf_counter() - started
    assert (
        masked.count(label) == len(replacements) == 40_000 * chunk.count("@" if label == "[почта]" else "999")
    )
    assert elapsed < 8.0, elapsed  # было 38 с для почты и 13 с для 20 000 телефонов


def test_line_numbers_stay_right_for_many_replacements():
    lines = [
        f"строка {n}: контакт n{n}@example.test" if n % 3 == 0 else f"строка {n}: без контактов"
        for n in range(3000)
    ]
    _, replacements, _ = safety.mask_personal("\n".join(lines))
    assert [r.line for r in replacements] == [n + 1 for n in range(3000) if n % 3 == 0]


def test_replacement_order_and_text_do_not_change_with_the_new_search():
    text = "ИНН 500100732259, почта a@b.ru, +7 (905) 123-45-67 и 8-800-555-35-35, СНИЛС 112-233-445 95"
    masked, replacements, categories = safety.mask_personal(text)
    assert masked == "ИНН [ИНН], почта [почта], [телефон] и 8-800-555-35-35, СНИЛС [СНИЛС]"
    assert [r.label for r in replacements] == ["[ИНН]", "[почта]", "[телефон]", "[СНИЛС]"]
    assert categories == ["номера документов и карт", "адреса почты", "телефоны"]


# --- штрихкоды EAN-13 не карты --------------------------------------------------------------------


def test_two_thousand_random_barcodes_are_never_taken_for_a_card():
    rng = random.Random(7)
    false = [
        code
        for code in (ean13(rng, rng.choice(["460", "590", "400", "500", "250"])) for _ in range(2000))
        if safety.mask_personal(f"Штрихкод {code} в каталоге")[1]
    ]
    assert false == []


@pytest.mark.parametrize("length", [14, 15, 16, 17, 18, 19])
def test_plain_card_numbers_of_fourteen_digits_and_more_are_still_replaced(length):
    number = luhn_number(random.Random(length), "4", length)
    masked, replacements, _ = safety.mask_personal(f"Карта {number} привязана")
    assert masked == "Карта [карта] привязана" and len(replacements) == 1


def test_grouped_card_of_any_length_with_luhn_is_still_replaced():
    number = luhn_number(random.Random(1), "4", 16)
    grouped = " ".join(number[i : i + 4] for i in range(0, 16, 4))
    assert safety.mask_personal(f"Карта {grouped}.")[0] == "Карта [карта]."


# --- страницы: СНИЛС с разделителями и карта группами ---------------------------------------------


def test_page_text_masks_a_separated_snils_and_a_grouped_card_with_a_check_sum():
    rng = random.Random(3)
    number = luhn_number(rng, "5", 16)
    grouped = " ".join(number[i : i + 4] for i in range(0, 16, 4))
    dashed = "-".join(number[i : i + 4] for i in range(0, 16, 4))
    page = f"Клиент: СНИЛС {snils(rng)}, карта {grouped} и запасная {dashed}."
    rep = safety.inspect_text(page)
    assert rep.text.strip() == "Клиент: СНИЛС [СНИЛС], карта [карта] и запасная [карта]."
    assert "номера документов и карт" in rep.anonymized


@pytest.mark.parametrize(
    "text",
    [
        "СНИЛС 112-233-445 96",  # неверное контрольное число
        "Карта 4111 1111 1111 1112",  # неверная сумма Луна
        "Заказ 1234 5678 9012 3456 принят",  # группы без суммы Луна
        "Версия 2026 1116 0001 0002",
    ],
)
def test_page_text_keeps_numbers_without_a_matching_check_sum(text):
    assert safety.inspect_text(text).text.strip() == text


def test_page_text_does_not_touch_inn_and_digits_in_a_row():
    rng = random.Random(5)
    page = (
        "ИП Иванов, ИНН 500100732259, ОГРНИП 304500116000157. "
        f"Карта слитно {luhn_number(rng, '4', 16)}, СНИЛС слитно 11223344595, "
        "трек-номер 1234567890123456, артикул 4601234567890."
    )
    assert safety.inspect_text(page).text.strip() == page


def test_material_path_is_unchanged_for_plain_numbers():
    rng = random.Random(9)
    number = luhn_number(rng, "4", 16)
    assert safety.mask_personal(f"Карта {number}")[0] == "Карта [карта]"
