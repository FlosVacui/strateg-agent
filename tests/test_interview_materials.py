"""Расшифровка интервью: признак «похоже на расшифровку» в обе стороны, путь исходника, отметка согласия.

Корпус собран отдельно от правил: пять расшифровок другого вида и восемь обычных материалов. Тексты выдуманы.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from docx import Document

from helpers import write_claims
from strateg import materials, safety
from strateg.project_check import check_project
from test_interview_add import CONSENT, add, demo, project_text, put, sources  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent

NOT_INTERVIEW = {
    "протокол.md": (
        "Протокол встречи команды 12.11\n"
        "Вопрос: сроки релиза.\nОтвет: переносим на декабрь.\n"
        "Вопрос: бюджет на рекламу.\nОтвет: остаётся прежним.\n"
        "Вопрос: кто ведёт закупку.\nОтвет: отдел закупок.\n"
    ),
    "faq.md": (
        "# Частые вопросы клиентов\n"
        "Вопрос: Как оплатить заказ?\nОтвет: Картой или по счёту.\n"
        "Вопрос: Можно ли вернуть товар?\nОтвет: Да, в течение 14 дней.\n"
        "Вопрос: Есть ли доставка в регионы?\nОтвет: Есть, через пункты выдачи.\n"
    ),
    "переписка с поддержкой.txt": (
        "Клиент: заказ не пришёл\nПоддержка: проверяем\nКлиент: третий день жду\nПоддержка: курьер опоздал\n"
    ),
    "сценарий звонка.md": (
        "Сценарий исходящего звонка\nОператор: Здравствуйте, удобно говорить?\n"
        "Вопрос 1: Пользуетесь ли вы доставкой?\nВопрос 2: Как часто?\nВопрос 3: Что не нравится?\n"
    ),
    "план интервью.md": "# Гайд\n1. Расскажите о последнем разе.\n2. Что было дальше?\n",
    "interview_guide.md": "# Guide\n1. Tell me about the last time.\n",
    "опрос респондентов.csv": "респондент;возраст;ответ\n1;34;да\n2;41;нет\n",
    "отчёт о UX-исследовании (транскрипты в приложении).md": "Итоги: людям неудобна оплата.\n",
}

REAL_INTERVIEW = {
    "встреча 12.11.txt": (
        "И: Расскажите о последнем заказе.\nР: В пятницу заказывали на пятерых.\n"
        "И: Что было дальше?\nР: Курьер опоздал на сорок минут.\nИ: И что вы сделали?\nР: Позвонил в кафе.\n"
    ),
    "звонок с клиентом.txt": (
        "Модератор: Расскажите о последнем заказе.\nКлиент: Заказывал в пятницу.\n"
        "Модератор: Сколько ждали?\nКлиент: Минут сорок.\nМодератор: Что сделали?\nКлиент: Позвонил.\n"
    ),
    "call notes.txt": (
        "Q: Tell me about the last order.\nA: Friday, five people.\nQ: What happened next?\n"
        "A: Courier was late.\nQ: And then?\nA: I called the cafe.\n"
    ),
    "разговор.txt": (
        "— Расскажите о последнем заказе.\n— Заказывал в пятницу, меня зовут Олег.\n"
        "— Сколько ждали?\n— Минут сорок, жена Светлана ругалась.\n"
        "— Что сделали?\n— Позвонил в кафе, потом жене.\n"
    ),
    "Анна_Смирнова.txt": (
        "Анна: заказывала в пятницу.\nЯ: сколько ждали?\nАнна: сорок минут.\nЯ: что сделали?\n"
        "Анна: позвонила.\nЯ: и потом?\nАнна: ждала.\nЯ: а кафе?\nАнна: извинилось.\n"
    ),
}


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[1]


@pytest.mark.parametrize("name", list(NOT_INTERVIEW))
def test_ordinary_material_is_never_refused_as_a_transcript(name):
    strong, _ = materials.transcript_signs(name, NOT_INTERVIEW[name], _ext(name))
    assert strong is False, name


@pytest.mark.parametrize("name", list(REAL_INTERVIEW))
def test_a_transcript_of_another_shape_is_refused_without_a_kind(name):
    strong, signs = materials.transcript_signs(name, REAL_INTERVIEW[name], _ext(name))
    assert strong is True and signs, name


def test_the_short_dialogue_through_dashes_is_only_a_warning():
    text = "— Расскажите о последнем заказе.\n— Заказывал в пятницу.\n— Сколько ждали?\n— Минут сорок.\n"
    strong, signs = materials.transcript_signs("разговор.txt", text, ".txt")
    assert strong is False and signs


@pytest.mark.parametrize("name", ["протокол.md", "faq.md", "план интервью.md", "опрос респондентов.csv"])
def test_weak_signs_warn_and_do_not_refuse(demo, cli, name):  # noqa: F811
    put(demo, name, NOT_INTERVIEW[name])
    code, out, err = cli("add", name)
    assert code == 0, err
    assert sources(demo)[0]["kind"] in ("заметка пользователя", "таблица")
    assert "по признакам это может быть расшифровка интервью" in out


def test_the_guide_of_the_product_is_accepted_without_a_kind(demo, cli):  # noqa: F811
    put(demo, "interview_guide.md", NOT_INTERVIEW["interview_guide.md"])
    assert cli("add", "interview_guide.md")[0] == 0


def test_a_refusal_names_the_kind_the_extension_allows(demo, cli):  # noqa: F811
    doc = Document()
    doc.add_paragraph("Просто текст.")
    buf = io.BytesIO()
    doc.save(buf)
    put(demo, "расшифровка.docx", buf.getvalue())
    code, _, err = cli("add", "расшифровка.docx")
    assert code == 1 and '--kind "документ"' in err and "--kind интервью" in err
    put(demo, "расшифровка встречи.txt", "Просто текст.\n")
    code, _, err = cli("add", "расшифровка встречи.txt")
    assert code == 1 and '--kind "заметка пользователя"' in err


def test_a_table_is_not_refused_as_a_transcript_even_with_the_word_in_the_name(demo, cli):  # noqa: F811
    put(demo, "расшифровка звонков.csv", "звонок;итог\n1;да\n")
    assert cli("add", "расшифровка звонков.csv")[0] == 0


def test_a_note_called_interview_goes_through_with_a_hint(demo, cli):  # noqa: F811
    put(demo, "интервью.txt", "Заказ пришёл поздно, звонила в поддержку по телефону +7 (905) 123-45-67.\n")
    code, out, err = cli("add", "интервью.txt")
    assert code == 0, err
    assert "по признакам это может быть расшифровка интервью" in out


# --- путь исходника ----------------------------------------------------------------------------------


def test_an_interview_without_recognised_personal_data_still_leaves_inputs(demo, cli):  # noqa: F811
    text = "Респондент: Меня зовут olga, пишите в телегу @olga_k_1987.\n"
    put(demo, "talk.txt", text)
    code, out, err = add(cli, "talk.txt", "--consent", CONSENT, "--json")
    assert code == 0, err
    s = sources(demo)[0]
    assert s["path"] == "inputs/i-1.txt" and s["text_path"] == "inputs/i-1.txt"
    assert "имена" in s["anonymized"] and not s.get("anonymization_confirmed")
    assert not (demo / "inputs" / "talk.txt").exists()
    assert (demo / ".tmp" / "pending" / "И-1" / "talk.txt").read_text(encoding="utf-8") == text
    assert any("не подтвердил замены" in e for e in check_project(demo))
    assert json.loads(out)["needs_confirmation"] is True


def test_a_surname_in_the_file_name_does_not_reach_the_project(demo, cli, private):  # noqa: F811
    put(demo, "интервью Смирнова Ольга.txt", "Респондент: Заказывали в пятницу.\n")
    code, _, err = add(cli, "интервью Смирнова Ольга.txt", "--consent", CONSENT)
    assert code == 0, err
    s = sources(demo)[0]
    assert s["path"] == "inputs/i-1.txt"
    assert "Смирнов" not in project_text(demo, skip_tmp=True)
    assert not list((demo / "inputs").glob("*Смирнов*"))
    assert cli("add", "--confirm", "И-1")[0] == 0
    assert "Смирнов" not in project_text(demo)
    assert (private / "demo" / "И-1" / "интервью Смирнова Ольга.txt").is_file()


def test_the_quote_waits_for_the_human_check_even_without_replacements(demo, cli, tmp_path):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Ждали курьера минут двадцать.\n")
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    claim = {
        "text": "Один респондент рассказал, что ждал курьера около двадцати минут",
        "type": "гипотеза",
        "subject": "ожидание курьера",
        "check_method": "спросить десять офис-менеджеров",
        "quotes": [
            {
                "source": "И-1",
                "text": "Ждали курьера минут двадцать",
                "publisher": "собеседник",
                "publisher_kind": "материал пользователя",
                "self_reported": True,
            }
        ],
    }
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [claim]))
    assert code == 1 and "не подтвердил замены" in err
    assert cli("add", "--confirm", "И-1")[0] == 0
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [claim]))
    assert code == 0, err


def test_the_hint_names_services_and_cities_and_the_confirm_command(demo, cli):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Живу в Королёве, плачу через Макс.\n")
    code, out, _ = add(cli, "talk.txt", "--consent", CONSENT)
    assert code == 0
    assert "названия сервисов и городов" in out and "strateg add --confirm И-1" in out


def test_latin_capitalised_words_are_a_warning_not_a_replacement(demo, cli):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Мне помог John Smith, а в Telegram писал ivan petrov.\n")
    code, out, _ = add(cli, "talk.txt", "--consent", CONSENT)
    assert code == 0
    assert "John" in out and "латиницей" in out
    assert "John" in (demo / sources(demo)[0]["text_path"]).read_text(encoding="utf-8")


def test_find_latin_names_ignores_acronyms_addresses_and_lowercase():
    found = [w for w, _ in safety.find_latin_names("API, john, a@Example.test, https://Site.test, Anna Lee")]
    assert found == ["Anna", "Lee"]


def test_the_consent_template_says_a_human_checks_the_copy():
    text = (ROOT / materials.CONSENT_TEMPLATE).read_text(encoding="utf-8")
    assert "обезличенную копию проверяет человек" in text


# --- отметка согласия --------------------------------------------------------------------------------


@pytest.mark.parametrize("consent", ["да 1", "получено 1 раз", "письменно 12.11", "устно 2026"])
def test_a_digit_is_not_a_date(demo, cli, consent):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Заказывали.\n")
    code, _, err = add(cli, "talk.txt", "--consent", consent)
    assert code == 1 and "нет даты" in err and sources(demo) == []


@pytest.mark.parametrize("consent", ["устно, 12.11.2026", "устно, 2026-11-12", "письменно 1.2.26"])
def test_dates_in_the_two_shapes_are_accepted(demo, cli, consent):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Заказывали.\n")
    assert add(cli, "talk.txt", "--consent", consent)[0] == 0


def test_a_latin_capitalised_word_in_the_mark_warns(demo, cli):  # noqa: F811
    put(demo, "talk.txt", "Респондент: Заказывали.\n")
    code, out, _ = add(cli, "talk.txt", "--consent", "письменно, John Smith, 12.11.2026")
    assert code == 0 and "в отметке согласия есть слова латиницей с заглавной" in out
    put(demo, "talk2.txt", "Респондент: Ехали.\n")
    code, out, _ = add(cli, "talk2.txt", "--consent", "письменно в чате, 12.11.2026")
    assert code == 0 and "слова латиницей" not in out


def test_check_consent_returns_the_mark_and_the_warnings():
    text, warnings = materials.check_consent("письменно  в  Telegram, 12.11.2026")
    assert text == "письменно в Telegram, 12.11.2026" and len(warnings) == 1
