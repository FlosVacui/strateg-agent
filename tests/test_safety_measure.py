"""Замер ADR-0011: доля найденного и ложные срабатывания по корпусу фикстур не хуже записанного в ADR.

Пороги — фактический результат на дату замера минус запас. Корпус фиксирован зерном, поэтому числа не плавают;
запас нужен на случай правки шаблонов: небольшая потеря не роняет тест, заметная — роняет. Имена — только
предупреждение: у них свой, более мягкий порог.
"""

from __future__ import annotations

import pytest

from safety_measure import measure

# вид -> (не меньше найдено, не больше ложных). Фактическое на 16.11.2026 — в комментарии.
THRESHOLDS = {
    "телефоны, add": (33, 0),  # 35 из 35, ложных 0 из 8
    "телефоны, fetch": (33, 0),  # 35 из 35, ложных 0 из 8
    "почта, add": (12, 0),  # 12 из 12, ложных 0 из 3
    "почта, fetch": (12, 0),  # 12 из 12, ложных 0 из 3
    "ИНН физлица, add": (10, 0),  # 10 из 10, ложных 0 из 20
    "СНИЛС, add": (10, 0),  # 10 из 10, ложных 0 из 5
    "карты, add": (19, 0),  # 20 из 20, ложных 0 из 10
    # штрихкоды российского EAN-13: 13 цифр, первая 4, сумма Луна подходит в одном случае из десяти:
    # известное ограничение, порог не даёт ему вырасти
    "штрихкоды EAN-13 «460…» (ложные карты), add": (0, 25),  # ложных 15 из 200
    "ключи, add": (20, 0),  # 20 из 20, ложных 0 из 12
    "ключи, fetch": (20, 0),  # 20 из 20, ложных 0 из 12
    "авторы отзывов, fetch": (12, 0),  # 12 из 12, ложных 0 из 6
    # имена — предупреждение: словарь ошибается в обе стороны, замены по ним нет
    "имена (предупреждение), add": (33, 4),  # 37 из 42 слов, ложных предупреждений 3 из 8
}


@pytest.fixture(scope="module")
def result():
    return measure()


def test_every_kind_has_a_threshold(result):
    assert set(result) == set(THRESHOLDS)


@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_found_and_false_are_not_worse_than_the_recorded_result(result, name):
    kind = result[name]
    min_found, max_false = THRESHOLDS[name]
    assert kind.found >= min_found, kind.row()
    assert kind.false <= max_false, kind.row()


def test_the_corpus_is_big_enough(result):
    assert sum(k.known for k in result.values()) >= 220
    assert sum(k.negatives for k in result.values()) >= 290


def test_names_are_counted_apart_and_never_replaced():
    from safety_measure import NAME_PHRASES
    from strateg import safety

    text = "Звонил " + NAME_PHRASES[0] + " и ещё " + NAME_PHRASES[3]
    masked, replacements, _ = safety.mask_personal(text)
    assert masked == text and replacements == []  # имя найдено словарём, но текст остаётся как есть
    assert safety.find_names(text)
