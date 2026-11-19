"""Утверждения по изображениям и снимкам экрана: цитата с местом «экран …; элемент …» (ADR-0005, ADR-0011)."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from helpers import FIXTURE, claims_of, write_claims
from strateg import classify
from strateg.imagequote import only_image
from strateg.models import ClaimsFile, Quote, behaviour_words, parse_image_locator
from strateg.project_check import check_project
from test_project_check import assert_error, claim, edit_yaml, errors_of

LOCATOR = "экран Оплата; элемент серая кнопка «Подтвердить» внизу справа"
QUOTE = "серая кнопка «Подтвердить» внизу справа"
OBSERVATION = "На экране оплаты кнопка «Подтвердить» серая"


def png_bytes(seed: int = 0) -> bytes:
    """Свой маленький PNG 4x4 без людей и чужих интерфейсов: цвет зависит от seed."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    row = b"\x00" + bytes([seed % 256, 120, 200]) * 4
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * 4))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def add_image(demo: Path, cli, name: str = "оплата.png", seed: int = 0, confirm: bool = True) -> None:
    path = demo / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(seed))
    assert cli("add", name)[0] == 0
    if confirm:
        assert cli("add", "--confirm", "И-1")[0] == 0


def fact(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "text": OBSERVATION,
        "type": "факт",
        "subject": "экран оплаты, кнопка «Подтвердить»",
        "quotes": [
            {
                "source": "И-1",
                "text": QUOTE,
                "locator": LOCATOR,
                "publisher": "автор снимка",
                "publisher_kind": "материал пользователя",
                "self_reported": False,
            }
        ],
    }
    data.update(extra)
    return data


def add_claim(demo: Path, cli, data: dict[str, Any], name: str = "c.yaml") -> tuple[int, str, str]:
    return cli("claim", "add", "--file", write_claims(demo / ".tmp", data, name))


def test_fact_about_the_interface_is_recorded_with_screen_and_element(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact())
    assert code == 0, err
    (c,) = claims_of(demo)
    (q,) = c["quotes"]
    assert q["locator"] == LOCATOR
    assert q["check"]["method"] == "по изображению" and q["check"]["similarity"] == 100
    assert q["check"]["snapshot_sha256"] == hashlib.sha256(png_bytes()).hexdigest()
    assert check_project(demo) == []


def test_quote_from_an_unconfirmed_image_is_refused_and_nothing_is_written(demo, cli):
    add_image(demo, cli, confirm=False)
    code, _, err = add_claim(demo, cli, fact())
    assert code == 1 and "не подтвердил" in err and "add --confirm И-1" in err
    assert claims_of(demo) == []
    assert any("на изображении нет персональных данных" in e for e in check_project(demo))
    assert cli("add", "--confirm", "И-1")[0] == 0
    assert add_claim(demo, cli, fact(), "c2.yaml")[0] == 0


@pytest.mark.parametrize(
    "locator",
    [
        None,
        "экран Оплата",
        "элемент кнопка",
        "экран ; элемент кнопка",
        "экран —; элемент кнопка",
        "экран Оплата; элемент —",
        "экран Оплата; элемент ",
        "экран Оплата; элемент —",
        "элемент кнопка; экран Оплата",
        "стр. 3",
        "экран Оплата, элемент кнопка",
    ],
)
def test_locator_is_required_and_parsed_strictly(demo, cli, locator):
    add_image(demo, cli)
    data = fact()
    if locator is None:
        del data["quotes"][0]["locator"]
    else:
        data["quotes"][0]["locator"] = locator
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "экран <название>; элемент <что и где>" in err
    assert claims_of(demo) == []


def test_empty_locator_is_refused_by_the_schema(demo, cli):
    add_image(demo, cli)
    data = fact()
    data["quotes"][0]["locator"] = ""
    assert add_claim(demo, cli, data)[0] == 1
    assert claims_of(demo) == []


def test_locator_parser():
    assert parse_image_locator("экран Оплата; элемент серая кнопка") == ("Оплата", "серая кнопка")
    assert parse_image_locator("  Экран  Главная ;  Элемент  меню слева ") == ("Главная", "меню слева")
    assert parse_image_locator("экран А; элемент Б; элемент В") is None
    assert parse_image_locator(None) is None


def test_fact_about_user_behaviour_is_refused_but_a_hypothesis_is_accepted(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact(text="Пользователи не находят кнопку «Подтвердить»"))
    assert code == 1 and "поведении или желаниях людей" in err and "гипотезой" in err
    assert claims_of(demo) == []
    assert add_claim(demo, cli, fact(), "c2.yaml")[0] == 0
    hyp = fact(
        text="Пользователи не находят кнопку «Подтвердить» из-за серого цвета",
        type="гипотеза",
        subject="заметность кнопки «Подтвердить»",
        check_method="пять интервью с показом экрана: найти кнопку за 10 секунд",
    )
    code, _, err = add_claim(demo, cli, hyp, "c3.yaml")
    assert code == 0, err
    assert [c["type"] for c in claims_of(demo)] == ["факт", "гипотеза"]
    assert check_project(demo) == []


# Корпус слов о людях и об интерфейсе: (текст, нужен ли отказ). Первые двенадцать — об интерфейсе, видимая
# подпись в «…»; остальные — выводы о людях, которых по снимку не видно.
BEHAVIOUR_CORPUS = [
    ("Для клиентов есть кнопка «Связаться» в шапке", False),
    ("На экране оплаты блок «Часто задаваемые вопросы» внизу", False),
    ("Поле «Предпочитаемое время доставки» без подсказки", False),
    ("Большинство полей формы помечены звёздочкой как обязательные", False),
    ("Ссылка «Участок и адрес» набрана серым мелким шрифтом", False),
    ("Кнопка «Как пользоваться сервисом» стоит под картинкой", False),
    ("Заголовок «Наши клиенты используют это каждый день» над кнопкой", False),
    ("Пункт меню «Редко заказываемое» свёрнут по умолчанию", False),
    ("Счётчик «Чаще всего покупают» стоит справа от корзины", False),
    ("Кнопка «Уходите? Оставьте почту» во всплывающем окне", False),
    ("Подпись «Необычной формы» под картинкой товара мелкая", False),
    ("Текст «Нажимая кнопку, вы соглашаетесь с условиями» под кнопкой", False),
    ("Посетители уходят с экрана оплаты", True),
    ("Конверсия на шаге оплаты низкая", True),
    ("Оформлять заказ неудобно", True),
    ("Пользователь уходит, не дойдя до оплаты", True),
    ("Покупатель бросает корзину на этом шаге", True),
    ("Клиенты жалуются на мелкий шрифт", True),
    ("Люди теряются на экране выбора тарифа", True),
    ("Пользователи не заметят серую кнопку", True),
    ("Человек не поймёт, что шаг обязательный", True),
    ("Пользователи ошибаются в поле даты", True),
    ("Пользователи запутаются в двух кнопках «Далее»", True),
    ("Сложно найти кнопку оплаты", True),
    ("Покупатели отказываются от оплаты картой", True),
    ("Пользователи не смогут вернуться на шаг назад", True),
    ("Кнопка раздражает пользователей", True),
    ("Пользователи не находят кнопку «Подтвердить»", True),
    ("Клиенты хотят платить сразу", True),
]


@pytest.mark.parametrize(("text", "refuse"), BEHAVIOUR_CORPUS, ids=[t for t, _ in BEHAVIOUR_CORPUS])
def test_behaviour_words_corpus(text, refuse):
    assert (behaviour_words(text) is not None) is refuse


def test_behaviour_corpus_has_both_sides():
    assert (
        sum(1 for _, r in BEHAVIOUR_CORPUS if r) == 17 and sum(1 for _, r in BEHAVIOUR_CORPUS if not r) == 12
    )


def test_behaviour_words():
    assert behaviour_words("Клиенты хотят платить сразу") == "хотят"
    assert behaviour_words("Большинство пользователей пропускает баннер")
    assert behaviour_words("На экране оплаты кнопка серая") is None
    assert behaviour_words("Пользователи часто бросают корзину")


def test_behaviour_words_skip_the_visible_label_and_match_from_the_word_start():
    assert behaviour_words("Кнопка «пользователи не находят подсказку» серая") is None  # подпись в «…»
    assert behaviour_words('Надпись "клиенты хотят скидку" мелкая') is None
    assert behaviour_words("Участок страницы с таблицей выделен рамкой") is None  # «часто» внутри слова
    assert behaviour_words("Участок страницы для клиентов выделен рамкой") is None  # рядом слово о людях
    assert behaviour_words("Необычной формы кнопка для пользователей серая") is None  # «обычно» внутри слова
    assert behaviour_words("Кнопка «Далее» несложно заметна на экране") is None  # «сложно» внутри слова
    assert behaviour_words("Меню уходит за край экрана") is None  # глагол без слова о людях
    assert behaviour_words("Клиенты хотят «скидку»") == "хотят"  # слова вне кавычек считаются


def test_assumption_about_people_from_an_image_is_refused_like_a_fact(demo, cli):
    add_image(demo, cli)
    data = fact(
        text="Пользователи не находят кнопку «Подтвердить»",
        type="допущение",
        taken_from="снимок экрана оплаты",
    )
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "поведении или желаниях людей" in err and claims_of(demo) == []
    ok = fact(text=OBSERVATION, type="допущение", taken_from="снимок экрана оплаты")
    assert add_claim(demo, cli, ok, "c2.yaml")[0] == 0


def test_check_flags_an_assumption_about_people_under_an_image(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact(text=OBSERVATION, type="допущение", taken_from="снимок экрана"))[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text="Покупатели бросают корзину"))
    assert any("поведении или желаниях людей" in e for e in check_project(demo))


def test_a_neutral_fact_whose_quote_speaks_about_people_is_refused(demo, cli):
    add_image(demo, cli)
    data = fact()
    data["quotes"][0]["text"] = "пользователи не находят кнопку подтверждения"
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "цитата со снимка говорит о поведении" in err
    data["quotes"][0]["text"] = "серая кнопка «Подтвердить» внизу справа, пользователи не находят подсказку"
    assert add_claim(demo, cli, data, "c2.yaml")[0] == 1
    data["quotes"][0]["text"] = (
        "серая кнопка «пользователи не находят подсказку» внизу справа"  # подпись в «…»
    )
    assert add_claim(demo, cli, data, "c3.yaml")[0] == 0


MARKET = "Рынок доставки обедов в Москве в 2026 году — 50 млрд рублей"


def test_market_fact_with_an_on_screen_element_is_refused(demo, cli):
    add_image(demo, cli)
    data = fact(text=MARKET, subject="объём рынка", period="2026")
    data["quotes"][0]["text"] = "рынок доставки обедов в 2026 году 50 млрд рублей"
    data["quotes"][0]["locator"] = "экран Главная; элемент баннер вверху"
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "не о том, что видно" in err and "гипотезой" in err
    assert claims_of(demo) == []


def test_fact_must_name_the_element_of_the_locator(demo, cli):
    add_image(demo, cli)
    data = fact(text="Сервис работает по всей России")
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "не называет элемент" in err and "серая кнопка" in err
    data = fact(text="На оплате есть серая кнопка")  # слова элемента есть в утверждении
    assert add_claim(demo, cli, data, "c2.yaml")[0] == 0


def test_what_the_label_says_may_be_recorded_in_quotes(demo, cli):
    add_image(demo, cli)
    data = fact(text="Баннер сообщает «Рынок доставки — 50 млрд рублей» крупным шрифтом")
    data["quotes"][0]["text"] = "баннер «Рынок доставки — 50 млрд рублей» крупным шрифтом"
    data["quotes"][0]["locator"] = "экран Главная; элемент баннер вверху страницы"
    code, _, err = add_claim(demo, cli, data)
    assert code == 0, err


def test_check_flags_a_market_fact_and_a_foreign_element_under_an_image(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text="Выручка сервиса за год выросла"))
    assert any("не о том, что видно" in e for e in check_project(demo))
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text="Сервис работает по всей России"))
    assert any("не называет элемент" in e for e in check_project(demo))


def test_check_flags_a_changed_snapshot_hash_even_if_the_file_is_whole(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(
        demo, "claims.yaml", lambda d: d["claims"][0]["quotes"][0]["check"].update(snapshot_sha256="0" * 64)
    )
    assert any("записана по другому файлу изображения" in e for e in check_project(demo))


def test_check_flags_an_image_source_without_the_registered_hash_and_a_missing_file(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    (demo / "inputs" / "оплата.png").unlink()
    assert any("не найден" in e for e in check_project(demo))


# --- смысл по снимку не сверяет и критик -------------------------------------------------------------------


def claims_file(demo: Path) -> ClaimsFile:
    return ClaimsFile.model_validate(
        __import__("yaml").safe_load((demo / "claims.yaml").read_text(encoding="utf-8"))
    )


def test_pairs_do_not_include_claims_from_a_screenshot(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    (c,) = claims_file(demo).claims
    assert only_image(c) and not classify.checkable(c)
    code, out, _ = cli("claim", "pairs", "--all")
    assert code == 0 and "Пар нет" in out
    code, _, err = cli("claim", "pairs", "--ids", c.id)
    assert code == 1 and "цитат со снимков экрана" in err
    code, _, err = cli(
        "claim", "verdict", c.id, "--verdict", "подтверждает", "--reason", "ок", "--by", "критик",
        "--model", "sonnet",
    )  # fmt: skip
    assert code == 1 and "цитат со снимков экрана" in err
    assert claims_of(demo)[0].get("verdict") is None


def test_status_says_that_the_meaning_of_a_screenshot_claim_is_not_checked(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    out = cli("status")[1]
    assert "со снимка экрана: 1" in out and "смысл по снимку код не проверяет" in out and "У-1" in out
    assert "смысл цитат проверен" not in out


def test_a_claim_with_a_text_quote_and_a_screenshot_quote_goes_to_the_check_with_the_text_one(project):
    def add_text_quote(d):
        c = claim(d, "У-9")
        c["quotes"].append(json.loads(json.dumps(claim(d, "У-2")["quotes"][0])))

    edit_yaml(project, "claims.yaml", add_text_quote)
    c = next(x for x in claims_file(project).claims if x.id == "У-9")
    assert classify.checkable(c)
    pair = classify.make_pair(c)
    assert [q.source for q in pair.quotes] == [c.quotes[1].source]


def test_image_changed_after_registration_is_refused(demo, cli):
    add_image(demo, cli)
    (demo / "inputs" / "оплата.png").write_bytes(png_bytes(7))
    code, _, err = add_claim(demo, cli, fact())
    assert code == 1 and "изменён после регистрации" in err
    assert claims_of(demo) == []


def test_image_quote_under_a_synthetic_claim_is_refused(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact(type="синтетика"))
    assert code == 1 and "синтетика" in err


def test_repeat_of_the_same_image_claim_is_not_a_second_record(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    code, _, _ = add_claim(demo, cli, fact(), "c2.yaml")
    assert code == 0 and len(claims_of(demo)) == 1


# --- check ловит ручные правки реестров ---------------------------------------------------------------


def test_check_flags_unconfirmed_image_under_an_existing_quote(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "sources.yaml", lambda d: d["sources"][0].update(no_personal_data_confirmed=False))
    errors = check_project(demo)
    assert any("claims.yaml" in e and "не подтвердил" in e and "нет персональных данных" in e for e in errors)


def test_check_names_an_image_that_was_replaced_under_the_same_name(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    (demo / "inputs" / "оплата.png").write_bytes(png_bytes(9))
    assert cli("add", "оплата.png")[0] == 0
    errors = check_project(demo)
    assert any("записан ещё раз как И-2" in e and "claims.yaml" in e for e in errors), errors


def test_check_flags_a_missing_or_broken_locator(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0]["quotes"][0].pop("locator"))
    assert any("claims.yaml" in e and "элемент <что и где>" in e for e in check_project(demo))
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0]["quotes"][0].update(locator="экран Оплата"))
    assert any("claims.yaml" in e and "элемент <что и где>" in e for e in check_project(demo))


def test_check_flags_a_changed_image_and_a_behaviour_fact(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text="Пользователи не находят кнопку"))
    assert any("поведении или желаниях людей" in e for e in check_project(demo))
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text=OBSERVATION))
    assert check_project(demo) == []
    (demo / "inputs" / "оплата.png").write_bytes(png_bytes(9))
    assert any("изменён после регистрации" in e for e in check_project(demo))


def test_model_requires_a_strict_locator_for_the_image_method():
    q: dict[str, Any] = {
        "source": "И-1",
        "text": QUOTE,
        "locator": LOCATOR,
        "check": {
            "method": "по изображению",
            "similarity": 100,
            "snapshot_sha256": "0" * 64,
            "checked_at": "2026-11-06T10:00:00+03:00",
        },
    }
    Quote.model_validate(q)
    q["locator"] = "стр. 2"
    with pytest.raises(ValueError, match="место записывается строго"):
        Quote.model_validate(q)


# --- пример проекта с изображением ----------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    body = png_bytes(3)
    (dest / "inputs").mkdir(exist_ok=True)
    (dest / "inputs" / "экран.png").write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()

    def add_source(d: dict[str, Any]) -> None:
        d["sources"].append(
            {
                "id": "И-9",
                "kind": "изображение",
                "path": "inputs/экран.png",
                "access": "получен",
                "untrusted": True,
                "material_sha256": sha,
                "no_personal_data_confirmed": True,
            }
        )

    def add_claims(d: dict[str, Any]) -> None:
        base = json.loads(json.dumps(claim(d, "У-2")))
        base.update(
            id="У-9",
            text=OBSERVATION,
            subject="экран оплаты, кнопка «Подтвердить»",
            replaces=None,
            plan_question=None,
        )
        base["quotes"] = [
            {
                "source": "И-9",
                "text": QUOTE,
                "locator": LOCATOR,
                "publisher": "автор снимка",
                "publisher_kind": "материал пользователя",
                "self_reported": False,
                "check": {
                    "method": "по изображению",
                    "similarity": 100,
                    "snapshot_sha256": sha,
                    "checked_at": "2026-11-06T10:20:00+03:00",
                },
            }
        ]
        base.pop("verdict", None)
        d["claims"].append(base)
        d["claims"].append(
            {
                "id": "У-10",
                "text": "Пользователи не находят серую кнопку «Подтвердить»",
                "type": "гипотеза",
                "origin": "агент",
                "step": "2б",
                "attempt": 1,
                "subject": "заметность кнопки «Подтвердить»",
                "basis": ["У-9"],
                "check_method": "пять интервью с показом экрана",
                "status": "действует",
                "status_history": [{"status": "действует", "at": "2026-11-06T10:21:00+03:00", "by": "агент"}],
            }
        )

    edit_yaml(dest, "sources.yaml", add_source)
    edit_yaml(dest, "claims.yaml", add_claims)
    return dest


def test_example_with_an_image_fact_is_clean(project):
    assert errors_of(project) == []


def test_image_method_on_a_text_source_is_an_error(project):
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: claim(d, "У-2")["quotes"][0].update(
            locator=LOCATOR, check={**claim(d, "У-2")["quotes"][0]["check"], "method": "по изображению"}
        ),
    )
    assert_error(project, "claims.yaml", "бывает только у цитаты с изображения")


def recommend(project, *claims: str) -> list[str]:
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=list(claims)))
    return [e for e in errors_of(project) if "только со снимков экрана" in e]


def put_in_plan(project, hypothesis: str) -> None:
    edit_yaml(project, "verification.yaml", lambda d: d["rows"][0].update(hypothesis=hypothesis))


def test_recommendation_on_image_facts_alone_needs_a_hypothesis(project):
    (error,) = recommend(project, "У-9")
    assert "options.yaml, recommendation" in error and "У-9" in error and "rows[].hypothesis" in error
    assert recommend(project, "У-9", "У-2") == []  # есть факт из другого источника: не «только со снимков»


def test_only_a_hypothesis_on_that_fact_from_the_plan_lifts_the_error(project):
    # У-10 опирается на У-9, но в плане проверки её нет
    assert recommend(project, "У-9", "У-10")
    put_in_plan(project, "У-10")
    assert recommend(project, "У-9", "У-10") == []
    assert recommend(project, "У-9")  # гипотезы в claims рекомендации нет: не снимает


def test_a_hypothesis_about_something_else_does_not_lift_the_error(project):
    # У-6 — гипотеза плана, но не о снимке: старое правило открывало любая гипотеза
    assert recommend(project, "У-9", "У-6")


def test_a_hypothesis_on_the_fact_through_a_chain_lifts_the_error(project):
    def add_next(d):
        h = json.loads(json.dumps(claim(d, "У-10")))
        h.update(
            id="У-11", text="Пользователи пропускают кнопку «Подтвердить» на экране оплаты", basis=["У-10"]
        )
        d["claims"].append(h)

    edit_yaml(project, "claims.yaml", add_next)
    put_in_plan(project, "У-11")
    assert recommend(project, "У-9", "У-11") == []


def test_an_assumption_does_not_lift_the_error(project):
    assert recommend(project, "У-9", "У-1")


def test_a_fact_only_from_a_screenshot_is_not_a_basis_of_a_decision_or_a_metric(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["basis"].append("У-9"))
    assert_error(project, "decisions.yaml", "поле basis", "У-9", "только на цитате со снимка экрана")
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["basis"].remove("У-9"))
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][3]["claims"].append("У-9"))
    assert_error(project, "gaps.yaml", "metric_coverage", "У-9", "показатель на снимке не измерен")
    # обещание честно строится на видимой цене или условии
    edit_yaml(project, "gaps.yaml", lambda d: d["metric_coverage"][3]["claims"].remove("У-9"))
    edit_yaml(project, "promises.yaml", lambda d: d["promises"][0]["claims"].append("У-9"))
    assert not any("promises.yaml" in e and "снимка экрана" in e for e in errors_of(project))


def test_a_hypothesis_on_the_fact_is_a_fine_basis_of_a_decision(project):
    edit_yaml(project, "decisions.yaml", lambda d: d["decisions"][0]["basis"].append("У-10"))
    assert not any("снимка экрана" in e for e in errors_of(project))


# --- документы и отказ для аудио и видео ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def test_docs_describe_images_and_the_limits():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Аудио и видео" in readme and "экран <название>; элемент <что и где>" in readme
    assert "Playwright не подключён" in readme
    adr5 = (ROOT / "docs" / "adr" / "0005-claims-quote-check.md").read_text(encoding="utf-8")
    assert "по изображению" in adr5 and "не признак вида источника" in adr5
    adr11 = (ROOT / "docs" / "adr" / "0011-data-safety.md").read_text(encoding="utf-8")
    assert "**Изображения.**" in adr11 and "add --confirm" in adr11
    assert "правило `ask`" in adr11 and "хук прав не даёт `Read` изображения" in adr11
    assert "Снимок, вставленный в чат" in adr11 and "в среде без хука" in adr11  # ограничение сказано прямо
    assert "модель его не видит" not in adr11
    assert "второй слой (проверка смысла критиком) к ней не применяется" in adr5
    assert "Агенту запрещено открывать его до `add --confirm`" in readme and "не давайте" in readme
    adr13 = (ROOT / "docs" / "adr" / "0013-mcp.md").read_text(encoding="utf-8")
    assert "разбор по снимкам экрана от человека сделан" in adr13
    schema = json.loads((ROOT / "schemas" / "claims.schema.json").read_text(encoding="utf-8"))
    assert "по изображению" in json.dumps(schema, ensure_ascii=False)
