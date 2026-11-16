# ruff: noqa: E501
"""Корпус для замера проверок до модели (ADR-0011, «Результат замера»).

Все данные вымышленные. Номера с контрольными суммами порождаются кодом с фиксированным зерном: корпус
один и тот же при каждом запуске. Значения ключей собираются из частей, чтобы сканер секретов репозитория не
срабатывал на сам корпус.

`measure()` считает по видам: сколько известных значений найдено (и заменено меткой) и сколько ложных срабатываний
на отрицательных примерах. Имена — предупреждение: считаются отдельно, замены по ним нет.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from strateg import fetch as fetchlib
from strateg import keys, safety

SEED = 20261116


# --- порождение номеров с верными контрольными суммами ------------------------------------------------------------


def _inn_digit(body: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(body, weights, strict=True)) % 11 % 10


def inn12(rng: random.Random) -> str:
    body = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(9)]
    body.append(_inn_digit(body, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)))
    body.append(_inn_digit(body, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)))
    return "".join(map(str, body))


def inn10(rng: random.Random) -> str:
    body = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
    body.append(_inn_digit(body, (2, 4, 10, 3, 5, 9, 4, 6, 8)))
    return "".join(map(str, body))


def bad_inn10(rng: random.Random) -> str:
    """Десять цифр с неверной контрольной суммой: номер заказа, а не ИНН."""
    while True:
        value = "".join(str(rng.randint(0, 9)) for _ in range(10))
        if not safety.inn_ok(value):
            return value


def snils(rng: random.Random) -> str:
    body = [rng.randint(0, 9) for _ in range(9)]
    total = sum(d * w for d, w in zip(body, range(9, 0, -1), strict=True))
    control = total if total < 100 else total % 101
    control = 0 if control in (100, 101) else control
    nine = "".join(map(str, body))
    return f"{nine[:3]}-{nine[3:6]}-{nine[6:]} {control:02d}"


def luhn_number(rng: random.Random, prefix: str, length: int = 16) -> str:
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(length - len(prefix) - 1))
    for check in "0123456789":
        if safety.luhn_ok(body + check):
            return body + check
    raise AssertionError(body)


def ean13(rng: random.Random, prefix: str = "460") -> str:
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(9))
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def phone_records(rng: random.Random) -> list[str]:
    out: list[str] = []
    for _ in range(6):
        code, a, b, c = (
            rng.choice(["905", "916", "926", "495", "812", "343"]),
            rng.randint(100, 999),
            rng.randint(10, 99),
            rng.randint(10, 99),
        )
        out += [
            f"+7 ({code}) {a}-{b}-{c}",
            f"8 {code} {a} {b} {c}",
            f"8-{code}-{a}-{b}-{c}",
            f"+7{code}{a}{b}{c}",
            f"8({code}){a}{b}{c}",
        ]
    out += [
        "+44 20 7946 0958",
        "+49 30 1234 5678",
        "+7 (495) 123-45-67",
        "(812) 555-66-77",
        "+375 29 123 45 67",
    ]
    return out


def email_records() -> list[str]:
    return [
        "ivan.petrov@example.test",
        "o.sidorova@mail.example.test",
        "support+tariff@example.test",
        "k_smirnov-77@sub.example.test",
        "anna@example.test",
        "info.team@dept.example.test",
        "n.lebedev@example.test",
        "user123@example.test",
        "maria.k@example.test",
        "p.egorov@kontora.example.test",
        "a-b.c_d@example.test",
        "denis@example.test",
    ]


# --- ключи -----------------------------------------------------------------------------------------------------------

_MIX = ("Ab1Cd2Ef3G" * 8)[:40]
KEY_VALUES = [
    "sk" + "-ant-" + "api03-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7",
    "sk" + "-" + "proj1234567890ABCDEFGH12",
    "gh" + "p_" + _MIX[:36],
    "gh" + "o_" + _MIX[:36],
    "gh" + "s_" + _MIX[:36],
    "github" + "_pat_" + "11ABCDEFG0abcdefghij1234567890",
    "gl" + "pat-" + "abcdefghij0123456789",
    "xo" + "xb-" + "1234567890-abcdefghij",
    "xo" + "xp-" + "1234567890-abcdefghij",
    "AI" + "za" + _MIX[:35],
    "123456789" + ":" + _MIX[:35],
    "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcDEF123_-xyzABC456",
    "AK" + "IA" + "IOSFODNN7EXAMPLE",
    "-----BEGIN " + "RSA PRIVATE KEY" + "-----",
    "Bearer " + "abcdef0123456789abcdef0123",
    "postgres://admin:" + "S3cr3tPassw0rd" + "@db.example.test:5432/app",
    "password=" + "Sup3rS3cretValue",
    "token=" + "abcdef0123456789abcdef",
    "Authorization: Basic " + "dXNlcjpwYXNzd29yZDEyMw==",
    "DB_PASSWORD=" + "Sup3rS3cretValue123",
    "sk" + "_live_" + "aB3dE5gH7jK9mN1pQ3sT5vW",
    "npm" + "_" + "aB3dE5gH7jK9mN1pQ3sT5vWaB3dE5gH7jK9mN",
    "hf" + "_" + "aB3dE5gH7jK9mN1pQ3sT5vWaB3dE5gH7jK9mN",
    "SG" + "." + "aB3dE5gH7jK9mN1pQ3sT5v" + "." + "aB3dE5gH7jK9mN1pQ3sT5vWaB3dE5gH7jK9mN",
    "AccountKey=" + "aB3dE5gH7jK9mN1pQ3sT5vWaB3dE5gH7jK9mNaB3dE5gH7j==",
    "api_key=" + "aB3dE5gH7jK9mN1pQ3sT5vW",
    "client_secret=" + "aB3dE5gH7jK9mN1pQ3sT5vW",
    "X-API-Key: " + "aB3dE5gH7jK9mN1pQ3sT5vW",
]
# Ключи в замере — по записи на каждое правило: число показывает отсутствие регрессии, а не полноту на неизвестных видах.
KEY_NEGATIVES = [
    "token=${GITHUB_TOKEN}",
    "token=<ваш токен>",
    "password=xxxxxxxxxxxx",
    "Authorization: Bearer ${TOKEN}",
    "postgres://user:${DB_PASSWORD}@db.example.test/app",
    "postgres://user:<password>@db.example.test/app",
    "ghp_xxxxxxxx",
    "ghp_" + "x" * 36,
    "sk-" + "0" * 30,
    "api_key=<ваш ключ>",
    "secret: подписка",
    "sk-подписка-на-год-и-месяц",
    "https://example.test:8080/path",
    "время 12:30:45",
    "Описание: токен — это строка, а пароль хранят отдельно.",
    "Bearer токен",
]

# --- авторы отзывов --------------------------------------------------------------------------------------------------

AUTHOR_NAME = "Олег Нестеров"
AUTHOR_HTML = [
    f'<div class="comment"><span class="comment-author">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<div class="comment"><span class="comment__author">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<div class="review"><span class="reviewer">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<div class="reviews"><b class="user-name">{AUTHOR_NAME}</b><p>Нормальная подписка.</p></div>',
    f'<div class="comments"><i class="nickname">{AUTHOR_NAME}</i><p>Нормальная подписка.</p></div>',
    f'<div class="comment" data-author="{AUTHOR_NAME}"><b>{AUTHOR_NAME}</b><p>Нормальная подписка.</p></div>',
    f'<div itemscope itemtype="https://schema.org/Review"><span itemprop="author">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<div itemscope itemtype="https://schema.org/Comment"><span itemprop="author">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<section class="comments"><div><span class="commenter">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div></section>',
    f'<div class="comments"><div class="comment"><cite>{AUTHOR_NAME}</cite><p>Нормальная подписка.</p></div></div>',
    f'<div class="h-entry comment"><span class="p-author h-card">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<ul class="comments"><li class="comment"><a class="u-url p-name" href="/u/1">{AUTHOR_NAME}</a><p>Нормальная подписка.</p></li></ul>',
    f'<div class="reviews"><div class="review"><a rel="author" href="/u/1">{AUTHOR_NAME}</a><p>Нормальная подписка.</p></div></div>',
    f'<div class="otzyv"><span class="author">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    f'<div class="testimonial"><span class="username">{AUTHOR_NAME}</span><p>Нормальная подписка.</p></div>',
    '<div class="comments"><p>@olegn согласен с автором, а @denis.v нет.</p></div>',
]
AUTHOR_NEGATIVE_HTML = [
    f'<article itemscope itemtype="https://schema.org/NewsArticle"><span itemprop="author">{AUTHOR_NAME}</span><p>Текст.</p></article>',
    f'<article><p class="byline"><span class="author">{AUTHOR_NAME}</span></p><p>Текст.</p></article>',
    f'<article data-author="{AUTHOR_NAME}"><p>Текст.</p></article>',
    f"<p>Министр {AUTHOR_NAME} заявил, что цены не вырастут. Компания «Вектор» подтвердила.</p>",
    f'<div class="post"><span class="username">{AUTHOR_NAME}</span><p>Заметка автора блога.</p></div>',
    "<p>Пишите на help@example.test или в канал @strateg_news.</p>",
    f'<div class="article-preview"><article><span class="author">{AUTHOR_NAME}</span><p>Текст.</p></article></div>',
    f'<div class="commentary"><article><span class="author">{AUTHOR_NAME}</span><p>Текст.</p></article></div>',
    f'<div class="comments-enabled"><article><span class="author">{AUTHOR_NAME}</span><p>Текст.</p></article></div>',
    f'<div class="review-article"><article><span class="author">{AUTHOR_NAME}</span><p>Текст.</p></article></div>',
    f'<div id="review-2026"><article><span class="author">{AUTHOR_NAME}</span><p>Текст.</p></article></div>',
    f'<div class="comments-wrap" data-author="{AUTHOR_NAME}"><article><p>Директор {AUTHOR_NAME} сообщил о планах.</p></article></div>',
]

# --- имена (предупреждение) --------------------------------------------------------------------------------------------

NAME_PHRASES = [
    "Иванов Пётр Сергеевич",
    "Петровой Анне Ивановне",
    "с Дмитрием Козловым",
    "Сидорова Олега",
    "Мария Кузнецова",
    "Алексея Смирнова",
    "Ольге Новиковой",
    "Татьяной Морозовой",
    "Николаю Волкову",
    "Елены Васильевой",
    "у Игоря Соколова",
    "Светлана Попова",
    "Андрею Лебедеву",
    "Юлией Семёновой",
    "Виктор Егоров",
    "Павлу Орлову",
    "Натальи Макаровой",
    "Сергея Зайцева",
    "Ирина Павлова",
    "Григорию Беляеву",
]
NAME_NEGATIVES = [
    "Роза ветров показывает направление",
    "Вера в успех проекта выросла",
    "Любовь к клиенту — основа сервиса",
    "Надежда на рост рынка остаётся",
    "Тариф Базовый подключён по умолчанию",
    "Московский регион и Санкт-Петербург",
    "Яндекс Маркет и Мегафон Премиум",
    "Подписка Защита стоит 199 рублей",
]


@dataclass
class Kind:
    name: str
    known: int = 0
    found: int = 0
    negatives: int = 0
    false: int = 0
    notes: list[str] = field(default_factory=list)

    def row(self) -> str:
        return f"{self.name}: найдено {self.found} из {self.known}; ложных {self.false} из {self.negatives}"


def _replaced(text: str) -> bool:
    masked, replacements, _ = safety.mask_personal(text)
    return bool(replacements) and masked != text


def measure() -> dict[str, Kind]:
    rng = random.Random(SEED)
    out: dict[str, Kind] = {}

    def kind(name: str) -> Kind:
        return out.setdefault(name, Kind(name))

    # телефоны: материал (add) и страница (fetch)
    phones = phone_records(rng)
    for fn, label in (
        (lambda t: _replaced(t), "телефоны, add"),
        (lambda t: safety.inspect_text(t).text.strip() != t.strip(), "телефоны, fetch"),
    ):
        k = kind(label)
        for ph in phones:
            k.known += 1
            k.found += bool(fn(f"Контакт для связи: {ph}, ждём ответа в течение дня."))
        negatives = [
            "Бесплатная линия: 8-800-555-35-35",
            "Звоните 8 800 100 00 01 в любое время",
            "Горячая линия +7 800 555 35 35",
            "Заказ № 1234567890 принят",
            "Договор № 77-123456/2026 от 2026-11-16",
            "Цена 1 299 000 рублей за год",
            "Артикул 4600123456 в наличии",
            "Версия 10.15.7.2026 вышла",
        ]
        for text in negatives:
            k.negatives += 1
            k.false += bool(fn(text))
    # почта: add и fetch
    for fn, label in (
        (lambda t: _replaced(t), "почта, add"),
        (lambda t: safety.inspect_text(t).text.strip() != t.strip(), "почта, fetch"),
    ):
        k = kind(label)
        for e in email_records():
            k.known += 1
            k.found += bool(fn(f"Пишите: {e} для связи"))
        for text in (
            "Версия 2.3 для Windows и macOS",
            "Логин вида имя собака домен не считается адресом",
            "Скидка 15% по промокоду ВЕСНА2026",
        ):
            k.negatives += 1
            k.false += bool(fn(text))
    # ИНН физлица, СНИЛС, карты: только add
    k = kind("ИНН физлица, add")
    for _ in range(10):
        k.known += 1
        k.found += _replaced(f"ИНН физлица {inn12(rng)}, подтверждён")
    for _ in range(10):
        k.negatives += 1
        k.false += _replaced(f"ИНН организации {inn10(rng)}, ООО «Вектор»")
    for _ in range(10):
        k.negatives += 1
        k.false += _replaced(f"Заказ № {bad_inn10(rng)} оформлен")
    k = kind("СНИЛС, add")
    for _ in range(10):
        k.known += 1
        k.found += _replaced(f"СНИЛС {snils(rng)} указан в анкете")
    for _ in range(5):
        k.negatives += 1
        k.false += _replaced(f"Номер {rng.randint(10**10, 10**11 - 1)} не СНИЛС")
    k = kind("карты, add")
    for _ in range(10):
        number = luhn_number(rng, rng.choice(["4", "51", "22", "37"]), 16)
        grouped = " ".join(number[i : i + 4] for i in range(0, 16, 4))
        k.known += 2
        k.found += _replaced(f"Карта {grouped} привязана") + _replaced(f"Карта {number} привязана")
    for _ in range(10):
        k.negatives += 1
        k.false += _replaced(f"Штрихкод {ean13(rng, '978')} в каталоге")
    k = kind("штрихкоды EAN-13 (ложные карты), add")  # 13 цифр, у каждого десятого подходит сумма Луна
    for _ in range(2000):
        k.negatives += 1
        k.false += _replaced(
            f"Штрихкод {ean13(rng, rng.choice(['460', '590', '400', '500', '250', '']))} в каталоге"
        )
    # страница: СНИЛС с разделителями и карта группами заменяются, остальные числа остаются
    k = kind("СНИЛС с разделителями, fetch")
    for _ in range(10):
        k.known += 1
        k.found += safety.inspect_text(f"Клиент СНИЛС {snils(rng)} в списке").text.count("[СНИЛС]")
    for _ in range(10):
        k.negatives += 1
        value = snils(rng)
        wrong = value[:-1] + str((int(value[-1]) + 1) % 10)
        plain = "".join(c for c in snils(rng) if c.isdigit())
        k.false += safety.inspect_text(f"СНИЛС {wrong}, слитно {plain}").text.count("[СНИЛС]")
    k = kind("карта группами, fetch")
    for _ in range(10):
        number = luhn_number(rng, rng.choice(["4", "51", "22", "37"]), 16)
        k.known += 2
        grouped = " ".join(number[i : i + 4] for i in range(0, 16, 4))
        dashed = "-".join(number[i : i + 4] for i in range(0, 16, 4))
        k.found += safety.inspect_text(f"Карта {grouped}").text.count("[карта]")
        k.found += safety.inspect_text(f"Карта {dashed}").text.count("[карта]")
    for _ in range(10):
        k.negatives += 4
        number = luhn_number(rng, "4", 16)
        k.false += safety.inspect_text(f"Слитно {number}").text.count(
            "[карта]"
        )  # цифры подряд на странице не трогаются
        k.false += safety.inspect_text(f"ИНН {inn12(rng)}").text.count("[ИНН]")
        bad = " ".join(f"{rng.randint(0, 9999):04d}" for _ in range(4))
        k.false += safety.inspect_text(
            f"Заказ {bad if not safety.luhn_ok(bad.replace(' ', '')) else '1234 5678 9012 3450'}"
        ).text.count("[карта]")
        k.false += safety.inspect_text(
            f"Версия {rng.randint(2020, 2030)} {rng.randint(1000, 9999)} 0001"
        ).text.count("[карта]")
    # ключи: add и fetch
    for fn, label in (
        (lambda t: _replaced(t), "ключи, add"),
        (lambda t: safety.inspect_text(t).text.strip() != t.strip(), "ключи, fetch"),
    ):
        k = kind(label)
        for value in KEY_VALUES:
            k.known += 1
            masked = (
                safety.inspect_text(f"в логе: {value} и дальше").text
                if "fetch" in label
                else safety.mask_personal(f"в логе: {value} и дальше")[0]
            )
            k.found += value not in masked and keys.KEY_LABEL in masked
        for text in KEY_NEGATIVES:
            k.negatives += 1
            k.false += bool(fn(text))
    # авторы отзывов: разметка страницы
    k = kind("авторы отзывов, fetch")
    for html in AUTHOR_HTML:
        k.known += 1
        out_html, _ = fetchlib.mask_authors(f"<html><body>{html}</body></html>")
        k.found += AUTHOR_NAME not in out_html and "@olegn" not in out_html and "@denis.v" not in out_html
    for html in AUTHOR_NEGATIVE_HTML:
        k.negatives += 1
        out_html, _ = fetchlib.mask_authors(f"<html><body>{html}</body></html>")
        text = fetchlib.visible_text(out_html)  # в тексте: метка в атрибуте разметки текста не меняет
        k.false += fetchlib.AUTHOR_LABEL in text or fetchlib.MENTION_LABEL in text
    # пробы разметки отзывов из независимого корпуса: рядом с замером «по записи на правило» это оценка на неизвестном
    from instruction_corpus_independent import AUTHOR_PROBES

    k = kind("авторы отзывов, пробы неизвестной разметки (независимый корпус), fetch")
    for _name, html, _was in AUTHOR_PROBES:
        k.known += 1
        out_html, _ = fetchlib.mask_authors(f"<html><body>{html}</body></html>")
        text = fetchlib.visible_text(out_html)
        k.found += "Иван Котов" not in text and "ivan_k" not in text
    # имена: только предупреждение
    k = kind("имена (предупреждение), add")
    for phrase in NAME_PHRASES:
        words = [w for w in phrase.split() if w[0].isupper()]
        found = {w for w, _ in safety.find_names(phrase)}
        k.known += len(words)
        k.found += sum(w in found for w in words)
    for text in NAME_NEGATIVES:
        k.negatives += 1
        k.false += bool(safety.find_names(text))
    return out
