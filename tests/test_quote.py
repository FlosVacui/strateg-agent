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
    q = "после подключения услуга предостовляется бесплатно, затем списывается абонентская плата"
    found = quote.check_quote(q, SNAPSHOT)
    assert found is not None and found.method == "нечёткое"
    assert found.similarity >= FUZZY_THRESHOLD


def test_fuzzy_rejects_changed_number():
    q = "Стоимость подписки в Москве — 149 рублей в месяц. Первые семь дней после подключения услуга"
    score = fuzz.partial_ratio_alignment(normalize_quote(q), normalize_quote(SNAPSHOT)).score
    assert score >= FUZZY_THRESHOLD, "один порог подмену цифры не ловит"
    assert quote.check_quote(q, SNAPSHOT) is None
    assert "числа, отрицания или слова" in quote.explain_miss(q, SNAPSHOT)


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
    assert quote.check_quote("предостав-ляется бес-платно", "услуга предоставляется бесплатно") is not None
    assert quote.check_quote("ляется бес-платно", "услуга предоставляется бесплатно") is None
    assert quote.check_quote("5 10 минут", "разговоров на 5-10 минут") is not None


@pytest.mark.parametrize(
    ("a", "b", "ok"),
    [
        ("предостовляется", "предоставляется", True),
        ("предоставляеться", "предоставляется", False),  # лишний знак: иначе «включена» и «выключена»
        ("платно", "бесплатно", False),
        ("возможно", "невозможно", False),
        ("доступна", "недоступна", False),
        ("антивирус", "вирус", False),
        ("нет", "net", False),
        ("199", "149", False),
        ("тариф", "тарив", False),  # опечатка на краю слова не допускается
    ],
)
def test_fuzzy_allows_only_typos_inside_a_word(a, b, ok):
    assert quote._typo(a, b) is ok


def test_fuzzy_allows_typo_but_not_added_or_dropped_word():
    text = "после подключения услуга предоставляется бесплатно, затем списывается абонентская плата."
    ok = "после подключения услуга предостовляется бесплатно, затем списывается абонентская плата"
    assert quote.check_quote(ok, text) is not None
    bad = "после подключения услуга не предоставляется бесплатно, затем списывается абонентская плата"
    assert quote.check_quote(bad, text) is None
    dropped = "после подключения услуга предостовляется бесплатно, затем списывается плата"
    assert quote.check_quote(dropped, text) is None  # пропущено слово «абонентская»


def test_fuzzy_allows_extra_text_at_the_edges():
    text = "Внимание. " + SNAPSHOT.splitlines()[0] + " Подробности на сайте."
    q = "после подключения услуга предостовляется бесплатно, затем списывается абонентская плата"
    assert quote.check_quote(q, text) is not None


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
def test_fuzzy_rejects_lost_unit_or_added_qualifier(snapshot, q):
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


def test_fuzzy_compares_sign_of_number():
    snapshot = "Изменение абонентской платы за год составило -5 % для всех действующих абонентов"
    q = "Изменение абонентской плвты за год составило 5 % для всех действующих абонентов"
    assert quote.check_quote(q, snapshot) is None


def test_fuzzy_checks_negation_at_the_edges_of_the_place():
    q = "абонентская плвта списывается ежемесячно со счёта абонента"
    assert quote.check_quote(q, "Абонентская плата списывается ежемесячно со счёта абонента.") is not None
    assert quote.check_quote(q, "Не абонентская плата списывается ежемесячно со счёта абонента.") is None
    assert (
        quote.check_quote(q, "Абонентская плата списывается ежемесячно со счёта абонента не будет.") is None
    )


@pytest.mark.parametrize(
    ("a", "b", "ok"),
    [
        ("тарифа", "тарфиа", False),  # перестановка — две замены, для короткого слова это много
        ("предоставляется", "предостовляется", True),
        ("предоставляется", "предостовлеется", True),  # две замены в длинном слове
        ("предоставляется", "предостовлеотся", False),  # три замены
        ("плата", "плвта", True),  # пять знаков — ещё слово
        ("план", "плен", False),  # четыре знака — уже нет
    ],
)
def test_typo_limits(a, b, ok):
    assert quote._typo(a, b) is ok


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
