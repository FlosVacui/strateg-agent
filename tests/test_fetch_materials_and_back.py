"""`fetch` и материалы из `inputs/`, возврат текста к прежнему снимку, `--refresh` без снимка (ADR-0004)."""

from __future__ import annotations

import json

import pytest

from helpers import URL, fact, journal_of, load_yaml, write_claims
from pdf_builder import make_pdf
from strateg.models import Source
from strateg.project_check import check_project

PDF_URL = "https://example.com/tariff.pdf"
TXT_URL = "https://example.com/tariff.txt"
TXT = "Тариф «Защита»: подписка стоит 199 рублей в месяц для всех абонентов в Москве, неделя бесплатно."
NEW_TEXT = (
    "<html><body><article><p>Стоимость подписки в Москве — 249 рублей в месяц. "
    "Первые семь дней бесплатно, дальше списывается абонентская плата. Подписка работает на всех тарифах."
    "</p></article></body></html>"
)


def fetch(cli, *argv: str) -> tuple[int, dict]:
    code, out, _ = cli("fetch", *argv, "--json")
    return code, json.loads(out)


def sources(root) -> list[dict]:
    return load_yaml(root / "sources.yaml")["sources"]


@pytest.fixture
def demo(cli, net, page_html, home):
    net.add(URL, page_html)
    assert cli("new", "demo")[0] == 0
    return home / "demo"


# --- Совпадение текста считается только среди веб-источников ------------------------------------------------


def test_a_page_with_the_text_of_a_pdf_material_gets_its_own_number_and_leaves_the_material_alone(
    cli, net, demo
):
    pdf = make_pdf(
        [
            "Tariff plan: subscription costs 199 rubles per month for all customers in Moscow region",
            "Second page: conditions apply to new subscribers and free trial lasts seven days",
        ]
    )
    net.add(PDF_URL, pdf, content_type="application/pdf")
    (demo / "inputs" / "tariff.pdf").write_bytes(pdf)
    assert cli("add", "inputs/tariff.pdf", "--project", "demo")[0] == 0
    before = sources(demo)[0]
    code, data = fetch(cli, PDF_URL)
    assert code == 0 and data["source"] == "И-2" and data["same_text_material"] == "И-1"
    assert "same_text_as" not in data
    assert any("текст совпадает с материалом И-1" in w for w in data["warnings"])
    assert sources(demo)[0] == before  # запись материала не менялась
    assert "other_urls" not in sources(demo)[0] or sources(demo)[0]["other_urls"] is None
    assert check_project(demo) == []
    assert cli("status", "--project", "demo")[0] == 0
    assert cli("check", "--project", "demo")[0] == 0


def test_a_page_with_the_text_of_a_txt_material_does_not_break_the_project(cli, net, demo):
    net.add(TXT_URL, TXT, content_type="text/plain; charset=utf-8")
    (demo / "inputs" / "tariff.txt").write_text(TXT + "\n", encoding="utf-8")
    assert cli("add", "inputs/tariff.txt", "--project", "demo")[0] == 0
    code, data = fetch(cli, TXT_URL)
    assert code == 0 and data["source"] == "И-2" and data["same_text_material"] == "И-1"
    text = cli("fetch", TXT_URL, "--project", "demo")[1]
    assert "И-2" in text  # повтор адреса — прежний номер
    assert check_project(demo) == []
    assert cli("status", "--project", "demo")[0] == 0
    assert cli("step", "start", "1", "--project", "demo")[0] == 0
    assert cli("step", "done", "1", "--project", "demo")[0] == 0


def test_a_second_address_still_joins_a_web_twin_but_not_the_material(cli, net, demo):
    net.add(TXT_URL, TXT, content_type="text/plain; charset=utf-8")
    net.add("https://example.com/copy.txt", TXT, content_type="text/plain; charset=utf-8")
    (demo / "inputs" / "tariff.txt").write_text(TXT + "\n", encoding="utf-8")
    cli("add", "inputs/tariff.txt", "--project", "demo")
    assert fetch(cli, TXT_URL)[1]["source"] == "И-2"
    code, data = fetch(cli, "https://example.com/copy.txt")
    assert data["source"] == "И-2" and data["same_text_as"] == "И-2"
    assert sources(demo)[1]["other_urls"] == ["https://example.com/copy.txt"]
    assert check_project(demo) == []


# --- Текст вернулся к прежнему снимку (A → B → A) ----------------------------------------------------------


def test_text_that_goes_back_to_an_earlier_snapshot_makes_no_second_source_with_it(
    cli, net, page_html, demo, clock
):
    fetch(cli, URL)
    first = sources(demo)[0]["text_sha256"]
    net.add(URL, NEW_TEXT)
    assert fetch(cli, URL, "--refresh")[1]["source"] == "И-2"
    net.add(URL, page_html)
    clock.advance(3600)
    code, data = fetch(cli, URL, "--refresh")
    assert (
        code == 0 and data["source"] == "И-1" and data["returned_to"] == "И-1" and data["refresh"] == "same"
    )
    assert data["sha256"] == first and data["rechecked_at"] == str(clock.value)
    assert [s["id"] for s in sources(demo)] == ["И-1", "И-2"]
    assert sources(demo)[0]["rechecked_at"] == clock.value.isoformat()
    assert sources(demo)[1].get("rechecked_at") is None
    assert check_project(demo) == []
    text = cli("fetch", URL, "--refresh", "--project", "demo")[1]
    assert "Источник И-" in text


def test_returning_text_is_named_in_the_text_answer(cli, net, page_html, demo):
    fetch(cli, URL)
    net.add(URL, NEW_TEXT)
    cli("fetch", URL, "--refresh")
    net.add(URL, page_html)
    code, out, _ = cli("fetch", URL, "--refresh")
    assert code == 0 and "страница вернулась к тексту этого снимка" in out and "Источник И-1" in out


# --- `--refresh` без прежнего снимка этого адреса и региона ------------------------------------------------


def test_refresh_without_a_snapshot_says_so(cli, net, demo):
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["refresh"] == "first"
    assert any("прежнего снимка нет, загружено как обычный fetch" in w for w in data["warnings"])


def test_refresh_in_another_region_is_a_plain_fetch_with_a_warning(cli, net, demo):
    assert fetch(cli, URL, "--region", "RU-SPE")[0] == 0
    code, data = fetch(cli, URL, "--refresh")  # регион проекта пуст: прежнего снимка в нём нет
    assert code == 0 and data["source"] == "И-2" and data["refresh"] == "first"
    assert any("прежнего снимка нет, загружено как обычный fetch" in w for w in data["warnings"])
    code, text, _ = cli("fetch", URL, "--region", "RU-SPE", "--refresh", "--project", "demo")
    assert code == 0 and "перепроверен" in text and "прежнего снимка нет" not in text
    assert len(sources(demo)) == 2 and check_project(demo) == []


def test_refresh_text_answer_carries_the_warning(cli, net, demo):
    code, out, _ = cli("fetch", URL, "--refresh", "--project", "demo")
    assert code == 0 and "Внимание: прежнего снимка нет, загружено как обычный fetch" in out


# --- Остальные условия перепроверки -------------------------------------------------------------------------


def test_robots_ban_on_refresh_is_written_as_skipped_not_as_an_error(cli, net, demo):
    fetch(cli, URL)
    net.add("https://example.com/robots.txt", "User-agent: *\nDisallow: /protection\n", "text/plain")
    net.t += 25 * 3600
    code, data = fetch(cli, URL, "--refresh")
    assert code == 1 and data["refresh"] == "failed"
    last = [e for e in journal_of(demo) if e.get("name") == "strateg fetch"][-1]
    assert last["status"] == "пропущено" and "не удалась" in last["message"]
    net.add("https://example.com/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
    net.t += 25 * 3600
    net.add(URL, status=500)
    assert fetch(cli, URL, "--refresh")[0] == 1
    assert [e for e in journal_of(demo) if e.get("name") == "strateg fetch"][-1]["status"] == "ошибка"


def test_claims_on_the_previous_snapshot_are_only_the_standing_ones(cli, net, demo, tmp_path):
    fetch(cli, URL)
    other = fact(
        "Первые семь дней после подключения услуга предоставляется бесплатно",
        text="Первую неделю услуга бесплатна",
        subject="пробный период",
    )
    cli("claim", "add", "--file", write_claims(tmp_path, [fact(), other]), "--project", "demo")
    assert cli("claim", "revoke", "У-2", "--reason", "проверка", "--project", "demo")[0] == 0
    net.add(URL, NEW_TEXT)
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["claims_on_previous"] == ["У-1"]


def test_a_source_cannot_refresh_itself():
    base = {
        "id": "И-1",
        "kind": "страница",
        "url": URL,
        "access": "источник недоступен: ошибка",
        "fetch": {"accessed_at": "2026-11-06T10:00:00+03:00"},
    }
    Source.model_validate(base)
    with pytest.raises(ValueError, match="сам себя"):
        Source.model_validate({**base, "refreshes": "И-1"})
