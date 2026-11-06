"""Проверка цитаты: способы по порядку, граница чисел, числа и отрицания в нечётком окне."""

import pytest
from rapidfuzz import fuzz

from strateg import quote
from strateg.models import FUZZY_THRESHOLD, normalize_quote

SNAPSHOT = (
    "Стоимость подписки в Москве — 199 рублей в месяц. Первые семь дней после подключения услуга "
    "предоставляется бесплатно, затем списывается абонентская плата.\n"
    "Подписка не включает страхование от финансовых потерь, а только предупреждает абонента "
    "о подозрительных вызовах и сообщениях.\n"
    "Услуга работает на всех тарифах оператора и подключается в личном кабинете за одну минуту."
)


def test_exact_after_normalization():
    found = quote.check_quote("СТОИМОСТЬ подписки   в Москве - 199 рублей в месяц", SNAPSHOT)
    assert found == quote.QuoteMatch("точное вхождение", 100.0)


def test_without_spaces_and_hyphens():
    found = quote.check_quote("предостав-ляется бес-платно, затем списывается", SNAPSHOT)
    assert found == quote.QuoteMatch("без пробелов и дефисов", 100.0)


def test_fuzzy_accepts_typo_in_long_quote():
    q = "Первые семь дней после подключения услуга предостовляется бесплатно, затем списывается плата"
    found = quote.check_quote(q, SNAPSHOT)
    assert found is not None and found.method == "нечёткое"
    assert found.similarity >= FUZZY_THRESHOLD


def test_fuzzy_rejects_changed_number():
    q = "Стоимость подписки в Москве — 149 рублей в месяц. Первые семь дней после подключения услуга"
    score = fuzz.partial_ratio_alignment(normalize_quote(q), normalize_quote(SNAPSHOT)).score
    assert score >= FUZZY_THRESHOLD, "один порог подмену цифры не ловит"
    assert quote.check_quote(q, SNAPSHOT) is None
    assert "числа или отрицания" in quote.explain_miss(q, SNAPSHOT)


@pytest.mark.parametrize(
    "q",
    [
        "Услуга не работает на всех тарифах оператора и подключается в личном кабинете за минуту",
        "Подписка включает страхование от финансовых потерь, а только предупреждает абонента",
    ],
)
def test_fuzzy_rejects_inserted_and_dropped_negation(q):
    score = fuzz.partial_ratio_alignment(normalize_quote(q), normalize_quote(SNAPSHOT)).score
    assert score >= FUZZY_THRESHOLD, "один порог отрицание не ловит"
    assert quote.check_quote(q, SNAPSHOT) is None


def test_short_quote_with_typo_is_not_checked_fuzzily():
    text = "Подписка помогает отсеивать звонки"
    assert quote.check_quote("Подписка помогвет отсеивать", text) is None
    assert "короче 40" in quote.explain_miss("Подписка помогвет отсеивать", text)


@pytest.mark.parametrize(
    ("q", "text"),
    [
        ("99 рублей", "Стоит 199 рублей в месяц"),
        ("990 рублей", "Стоит 1 990 рублей в месяц"),
        ("5 рублей", "Стоит 0,5 рублей"),
        ("стоит 99", "Стоит 995 рублей"),
        ("стоит 1", "Стоит 1,5 рубля"),
        ("стоит 1", "Стоит 1 990 рублей"),
    ],
)
def test_number_must_not_be_part_of_another_number(q, text):
    assert quote.check_quote(q, text) is None


def test_number_boundary_holds_for_long_quote_in_fuzzy_stage():
    text = "Цена 199 рублей в месяц. Первые семь дней после подключения услуга бесплатна для всех."
    q = "99 рублей в месяц. Первые семь дней после подключения услуга бесплатна для всех."
    assert quote.check_quote(q, text) is None


def test_number_boundary_does_not_block_normal_quotes():
    text = "С 1 января 2026 года цена 99 рублей, а в 2027 году — 120 рублей."
    assert quote.check_quote("цена 99 рублей", text) is not None
    assert quote.check_quote("в 2027 году — 120 рублей", text) is not None
    assert quote.check_quote("цена 99 рублей, а в 2027", text) is not None


def test_quote_without_letters_or_digits_is_never_found():
    assert quote.check_quote("— …", SNAPSHOT) is None


def test_pages_are_split_and_found():
    text = "[стр. 1]\nВведение и общие слова.\n\n[стр. 2]\nДоля абонентов вырастет с 12 до 15–17 процентов."
    assert [n for n, _ in quote.split_pages(text)] == [1, 2]
    assert quote.find_page("вырастет с 12 до 15-17 процентов", text) == 2
    assert quote.find_page("такого нет нигде в тексте документа", text) is None


def test_quote_cut_after_a_negation_is_refused():
    text = "Мы не пропускаем вызовы с подозрительных номеров и определяем подделанные голоса. Всё."
    assert quote.check_quote("пропускаем вызовы с подозрительных номеров", text) is None
    assert "отрицание" in quote.explain_miss("пропускаем вызовы с подозрительных номеров", text)
    assert quote.check_quote("не пропускаем вызовы с подозрительных номеров", text) is not None
    # слово «не» внутри другого слова отрицанием не считается
    assert (
        quote.check_quote("вызовы с подозрительных номеров", "Это небольшие вызовы с подозрительных номеров")
        is not None
    )
