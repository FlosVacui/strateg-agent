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
    # штрихкоды EAN-13 (13 цифр) картой не считаются: карта без разделителей — от 14 цифр
    "штрихкоды EAN-13 (ложные карты), add": (0, 0),  # ложных 0 из 2000
    "СНИЛС с разделителями, fetch": (10, 0),  # 10 из 10, ложных 0 из 10
    "карта группами, fetch": (19, 0),  # 20 из 20, ложных 0 из 40
    # ключи и авторы в замере — по записи на правило: число показывает отсутствие регрессии, а не полноту
    "ключи, add": (28, 0),  # 28 из 28, ложных 0 из 16
    "ключи, fetch": (28, 0),  # 28 из 28, ложных 0 из 16
    "авторы отзывов, fetch": (16, 0),  # 16 из 16, ложных 0 из 12
    # пробы неизвестной разметки (корпус другого составителя): разметка вне списка не находится
    "авторы отзывов, пробы неизвестной разметки (независимый корпус), fetch": (9, 0),  # 9 из 12
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
    assert sum(k.known for k in result.values()) >= 270
    assert sum(k.negatives for k in result.values()) >= 2000


def test_names_are_counted_apart_and_never_replaced():
    from safety_measure import NAME_PHRASES
    from strateg import safety

    text = "Звонил " + NAME_PHRASES[0] + " и ещё " + NAME_PHRASES[3]
    masked, replacements, _ = safety.mask_personal(text)
    assert masked == text and replacements == []  # имя найдено словарём, но текст остаётся как есть
    assert safety.find_names(text)
