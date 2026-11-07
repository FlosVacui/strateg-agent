"""Проверка цитаты: способы по порядку, граница слов, чисел и блоков, отрицания рядом с цитатой."""

from pathlib import Path

import pytest

from strateg import quote

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


def test_there_is_no_fuzzy_path():
    """Нечёткого совпадения нет: на пробе в 52 цитатах живых PDF оно не понадобилось ни разу."""
    q = "после подключения услуга предостовляется бесплатно, затем списывается абонентская плата"
    assert quote.check_quote(q, SNAPSHOT) is None  # опечатка в слове
    assert "такого текста в снимке нет" in quote.explain_miss(q, SNAPSHOT)
    changed = "Стоимость подписки в Москве — 149 рублей в месяц. Первые семь дней после подключения услуга"
    assert quote.check_quote(changed, SNAPSHOT) is None  # подменённая цифра
    inserted = "Услуга не работает на всех тарифах оператора и подключается в личном кабинете за минуту"
    assert quote.check_quote(inserted, SNAPSHOT) is None  # вставленное «не»
    assert not hasattr(quote, "fuzz")
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "rapidfuzz" not in pyproject


def test_short_quote_is_checked_only_by_exact_match():
    snapshot = "Услуга предоставляется бесплатно."
    assert quote.check_quote("предоставляется бесплатно", snapshot) is not None
    # перенос слова с дефисом допустим только для цитаты от 40 знаков
    assert quote.check_quote("предостав-ляется бес-платно", snapshot) is None
    assert "короче 40" in quote.explain_miss("предостав-ляется бес-платно", snapshot)


def test_short_quote_with_typo_is_not_found():
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


def test_number_boundary_holds_for_long_quote():
    text = "Цена 199 рублей в месяц. Первые семь дней после подключения услуга бесплатна для всех."
    q = "99 рублей в месяц. Первые семь дней после подключения услуга бесплатна для всех."
    assert quote.check_quote(q, text) is None


def test_number_boundary_does_not_block_normal_quotes():
    text = "С 1 января 2026 года цена 99 рублей, а в 2027 году — 120 рублей."
    assert quote.check_quote("цена 99 рублей", text) is not None
    assert quote.check_quote("в 2027 году — 120 рублей", text) is not None
    assert quote.check_quote("цена 99 рублей, а в 2027 году", text) is not None


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


# --- случаи ревью: искажённая цитата не должна проходить ---

S1 = (
    "Подключение услуги осуществляется бесплатно через личный кабинет. Это возможно только для абонентов "
    "Москвы, остальным подключение невозможно по техническим причинам."
)
S2 = "Услуга недоступна в сети партнёров, а также абонентам корпоративных тарифов. Скорость до 100 Мбит/с."

DISTORTED = [
    # граница слова: цитата начинается в середине слова и меняет смысл на обратный
    pytest.param("платно через личный кабинет", S1, id="бес|платно"),
    pytest.param("лимитный интернет на тарифе", "Безлимитный интернет на тарифе", id="без|лимитный"),
    pytest.param(
        "доступно для всех абонентов", "Услуга недоступно для всех абонентов региона", id="не|доступно"
    ),
    pytest.param("интернет на тарифе безлимит", "интернет на тарифе безлимитный", id="конец слова"),
    # нечёткое совпадение: приставка не-, бес-, без- меняет смысл
    pytest.param(
        "Услуга доступна в сети партнёров, а также абонентам корпоративных тарифов.",
        S2,
        id="недоступна->доступна",
    ),
    pytest.param(
        "Подключение услуги осуществляется бесплатно через личный кабинет. Это возможно только для "
        "абонентов Москвы, остальным подключение возможно по техническим причинам.",
        S1,
        id="невозможно->возможно",
    ),
    pytest.param(
        "Подключение услуги осуществляется платно через личный кабинет. Это возможно только для абонентов "
        "Москвы, остальным подключение невозможно",
        S1,
        id="бесплатно->платно",
    ),
    pytest.param(
        "Скорость интернета на тарифе безлимитном без ограничений по трафику",
        "Скорость интернета на тарифе лимитном с ограничением по трафику",
        id="другое слово",
    ),
    # граница числа: знак, диапазон, степень, буква или № перед числом
    pytest.param("5 % в месяц", "Снижение −5 % в месяц", id="знак минус"),
    pytest.param("5 % в месяц", "Снижение +5 % в месяц", id="знак плюс"),
    pytest.param("300 рублей в месяц", "Тариф стоит 200-300 рублей в месяц", id="хвост диапазона"),
    pytest.param("тариф на 200", "тариф на 200-300 минут", id="голова диапазона 2"),
    pytest.param("510 минут", "разговоров на 5-10 минут", id="5-10 -> 510"),
    pytest.param("11 рублей", "стоит 1 1 рублей", id="1 1 -> 11"),
    pytest.param("103 рубля", "стоит 10³ рубля", id="NFKC: 10³ -> 103"),
    pytest.param("99 рублей", "модель x99 рублей", id="буква перед числом"),
    pytest.param("5 рублей", "комната №5 рублей", id="№ перед числом"),
]


@pytest.mark.parametrize(("q", "snap"), DISTORTED)
def test_distorted_quote_is_rejected(q, snap):
    assert quote.check_quote(q, snap) is None


def test_quote_with_superscript_matches_itself():
    assert quote.check_quote("стоит 10³ рубля", "стоит 10³ рубля") is not None


def test_quote_at_word_boundaries_is_accepted():
    text = "Подключение бесплатно. Услуга доступна для всех абонентов, «лимитный» тариф."
    assert quote.check_quote("бесплатно", text) is not None
    assert quote.check_quote("лимитный", text) is not None
    assert quote.check_quote("абонентов, «лимитный»", text) is not None


def test_compact_view_keeps_words_and_digits_apart():
    text = "Первые семь дней услуга предоставляется бесплатно, затем списывается абонентская плата."
    assert quote.check_quote("услуга предостав-ляется бес-платно, затем списывается", text) is not None
    assert quote.check_quote("ляется бес-платно, затем списывается абонентская плата", text) is None
    long = "Разговоров на 5-10 минут в день хватает большинству абонентов оператора"
    assert (
        quote.check_quote("Разговоров на 5 10 минут в день хватает большинству абонентов", long) is not None
    )
    assert quote.check_quote("Разговоров на 510 минут в день хватает большинству абонентов", long) is None


# --- границы блоков: отрицание не переходит через строку ---

HEADED = (
    "Тарифы\nНет скрытых платежей и комиссий при подключении услуги\nНе рекомендуется\nПодписка бесплатна"
)


def test_heading_is_not_refused_for_negation_in_the_next_block():
    # «Нет…» в начале следующей строки — другой блок, а не отрицание заголовка
    assert quote.check_quote("Тарифы", HEADED) is not None
    assert (
        quote.check_quote("Подписка бесплатна", HEADED) is not None
    )  # «Не рекомендуется» выше — другой блок


def test_negation_in_the_same_line_is_still_refused():
    line = "Тарифы нет скрытых платежей\nДругая строка"
    assert quote.check_quote("Тарифы", line) is None
    assert "отрицание" in quote.explain_miss("Тарифы", line)
    assert quote.check_quote("скрытых платежей", line) is None  # «нет» слева в той же строке
    assert quote.check_quote("нет скрытых платежей", line) is not None


def test_block_boundary_also_cuts_the_left_side_and_numbers_keep_their_rules():
    text = "Не включено\nПодписка 199 рублей"
    assert quote.check_quote("Подписка 199 рублей", text) is not None
    assert quote.check_quote("99 рублей", text) is None


def test_pdf_lines_are_soft_wraps_so_negation_on_the_next_line_still_counts():
    page = "[стр. 1]\nУслуга доступна всем абонентам\nне только корпоративным"
    assert quote.pages_with_quote("Услуга доступна всем абонентам", page) == []
    # в тексте не из PDF та же пара строк — два блока
    assert quote.check_quote("Услуга доступна всем абонентам", "Услуга доступна всем абонентам\nне только")
    # пустая строка в PDF — граница блока
    spaced = "[стр. 1]\nЗаголовок раздела\n\nНет скрытых платежей"
    assert quote.pages_with_quote("Заголовок раздела", spaced)


CHANGED_MEANING = [
    pytest.param(
        "Стоимость подписки составляет 249 тыс. руб. в месяц для абонентов корпоративного тарифа оператора",
        "Стоимость подписки составляет 249 руб. в месяц для абонентов корпоративного тарифа оператора",
        id="тыс. потеряно",
    ),
    pytest.param(
        "Количество блокированных звонков составило 3 млн штук за первый квартал после запуска услуги.",
        "Количество блокированных звонков составило 3 штук за первый квартал после запуска услуги.",
        id="млн потеряно",
    ),
    pytest.param(
        "Доля мошеннических звонков снизилась на 5 п.п. за первый год после запуска услуги у оператора.",
        "Доля мошеннических звонков снизилась на 5 % за первый год после запуска услуги у оператора.",
        id="п.п. вместо %",
    ),
    pytest.param(
        "Подписка стоит 249 рублей в год для абонентов корпоративного тарифа оператора связи России сейчас.",
        "Подписка стоит 249 рублей для абонентов корпоративного тарифа оператора связи России сейчас.",
        id="период потерян",
    ),
    pytest.param(
        "Подключение услуги для абонентов осуществляется через личный кабинет оператора связи "
        "в регионах присутствия компании.",
        "Подключение услуги для абонентов осуществляется только через личный кабинет оператора связи "
        "в регионах присутствия компании.",
        id="слово добавлено",
    ),
    pytest.param(
        "Услуга доступна абонентам тарифа в первый месяц после подключения и далее по тарифу "
        "оператора связи.",
        "Услуга доступна абонентам любого тарифа в первый месяц после подключения и далее по тарифу "
        "оператора связи.",
        id="слово добавлено рядом",
    ),
]


@pytest.mark.parametrize(("snapshot", "q"), CHANGED_MEANING)
def test_lost_unit_or_added_qualifier_is_not_found(snapshot, q):
    assert quote.check_quote(q, snapshot) is None


def test_negation_within_three_words_in_same_sentence():
    text = (
        "Мы не обещаем и не гарантируем, что услуга всегда работает. Услуга работает по расписанию вечером."
    )
    assert (
        quote.check_quote("гарантируем, что услуга всегда работает", text) is None
    )  # не — в трёх словах слева
    assert (
        quote.check_quote("услуга работает по расписанию вечером", text) is not None
    )  # в другом предложении
    far = "Не секрет, что услуга давно работает по расписанию вечером"
    assert quote.check_quote("давно работает по расписанию вечером", far) is not None  # «не» дальше трёх слов


def test_numbers_helper_keeps_sign_and_ranges():
    assert quote.numbers("-5 % и +3, 200-300, 1 990 руб, 12,5") == ["+3", "-5", "12.5", "1990", "200", "300"]


@pytest.mark.parametrize(
    "tail",
    [
        ", кроме корпоративных тарифов.",
        ", не будет предоставляться всем.",
        " нет, только по запросу.",
    ],
)
def test_negation_or_exception_right_after_quote_is_refused(tail):
    text = "Услуга доступна всем абонентам региона присутствия оператора" + tail
    q = "Услуга доступна всем абонентам региона присутствия оператора"
    assert quote.check_quote(q, text) is None
    assert "отрицание" in quote.explain_miss(q, text)


def test_negation_in_next_sentence_or_far_right_is_not_a_reason():
    q = "Услуга доступна всем абонентам региона присутствия оператора"
    assert (
        quote.check_quote(q + ".", q + ". Не отключается без вашего согласия.") is not None
    )  # точка в цитате
    assert quote.check_quote(q, q + ". Не отключается без вашего согласия.") is not None
    assert quote.check_quote(q, q + " в течение месяца после подключения, не меняется") is not None


@pytest.mark.parametrize(
    ("snapshot", "q"),
    [
        ("Тариф действует в 2025/2026 учебном году для абонентов", "2026 учебном году для абонентов"),
        ("Поддержка работает 24/7 дней в неделю для абонентов", "7 дней в неделю для абонентов"),
        ("Скидка действует 1/2 месяца после подключения услуги", "2 месяца после подключения услуги"),
        ("Оператор отвечает в течение 10:30 минут после обращения", "30 минут после обращения"),
    ],
)
def test_number_joined_by_slash_or_colon_is_one_number(snapshot, q):
    assert quote.check_quote(q, snapshot) is None


def test_whole_number_with_slash_or_colon_is_found():
    assert (
        quote.check_quote("действует в 2025/2026 учебном году", "Тариф действует в 2025/2026 учебном году")
        is not None
    )
    assert quote.check_quote("отвечает в 10:30 минут", "Оператор отвечает в 10:30 минут после") is not None


@pytest.mark.parametrize(
    ("snapshot", "q"),
    [
        (
            "Услуга защиты выключена по умолчанию для всех абонентов региона",
            "Услуга защиты включена по умолчанию для всех абонентов региона",
        ),
        (
            "Услуга защиты включена в тариф по умолчанию для всех абонентов",
            "Услуга защиты выключена в тариф по умолчанию для всех абонентов",
        ),
        (
            "Автоматическое выключение услуги защиты происходит при подключении тарифа",
            "Автоматическое включение услуги защиты происходит при подключении тарифа",
        ),
        (
            "Плата за услугу защиты выходит из стоимости тарифа для всех абонентов",
            "Плата за услугу защиты входит из стоимости тарифа для всех абонентов",
        ),
    ],
)
def test_one_letter_antonym_is_not_a_typo(snapshot, q):
    assert quote.check_quote(q, snapshot) is None


@pytest.mark.parametrize(
    ("snapshot", "q"),
    [
        (
            "Стоимость тарифа для абонентов региона составляет 249 тыс. ₽",
            "Стоимость тарифа для абонентов региона составляет 249 ₽",
        ),
        (
            "Рост доли подключений к услуге за год составил 5 п.п.",
            "Рост доли подключений к услуге за год составил 5 %",
        ),
        (
            "Стоимость тарифа для абонентов региона составляет 249 ₽ в месяц",
            "Стоимость тарифа для абонентов региона составляет 249 $ в месяц",
        ),
        (
            "Стоимость тарифа для абонентов региона составляет 249 $ в месяц",
            "Стоимость тарифа для абонентов региона составляет 249 € в месяц",
        ),
    ],
)
def test_currency_and_percent_signs_are_compared(snapshot, q):
    assert quote.check_quote(q, snapshot) is None


@pytest.mark.parametrize("tail", ["тыс. рублей в месяц", "₽ в месяц", "рублей в месяц", "% в месяц"])
def test_quote_does_not_stop_on_number_before_its_unit(tail):
    snapshot = f"Стоимость тарифа для абонентов составляет 249 {tail}."
    cut = "Стоимость тарифа для абонентов составляет 249"
    assert quote.check_quote(cut, snapshot) is None
    assert "единица" in quote.explain_miss(cut, snapshot)
    assert quote.check_quote(f"{cut} {tail}", snapshot) is not None


def test_quote_may_stop_on_number_at_end_of_sentence():
    snapshot = "Цена на тариф составляет 249. Дальше текст"
    assert quote.check_quote("Цена на тариф составляет 249", snapshot) is not None


def test_sign_of_number_is_compared():
    snapshot = "Изменение абонентской платы за год составило -5 % для всех действующих абонентов"
    q = "Изменение абонентской плвты за год составило 5 % для всех действующих абонентов"
    assert quote.check_quote(q, snapshot) is None


@pytest.mark.parametrize(
    ("snapshot", "q", "ok"),
    [
        ("Цена тарифа стала выше обычной, и не более того.", "Цена тарифа стала выше обычной", False),
        ("Цена тарифа стала выше обычной сейчас не более того.", "Цена тарифа стала выше обычной", False),
        ("Цена тарифа стала выше обычной сейчас и потом не более.", "Цена тарифа стала выше обычной", True),
    ],
)
def test_right_side_looks_at_two_words(snapshot, q, ok):
    assert (quote.check_quote(q, snapshot) is not None) is ok


@pytest.mark.parametrize(
    ("snapshot", "q"),
    [
        ("Срок действия 2025/2026 года для всех абонентов", "Срок действия 2025 года для всех абонентов"),
        ("Режим работы 10:30 ежедневно для всех абонентов", "Режим работы 10 ежедневно для всех абонентов"),
    ],
)
def test_number_joined_on_the_right_is_one_number(snapshot, q):
    assert quote.check_quote(q, snapshot) is None


@pytest.mark.parametrize(
    "text",
    [
        "Тем не менее, по оценке аналитиков, ущерб за год превысил 600 млрд руб.",
        "Ущерб за год превысил 600 млрд руб, тем не менее рост замедлился.",
    ],
)
def test_turn_of_phrase_is_not_a_negation(text):
    q = (
        "по оценке аналитиков, ущерб за год превысил 600 млрд руб"
        if text.startswith("Тем")
        else "ущерб за год превысил 600 млрд руб"
    )
    assert quote.check_quote(q, text) is not None
