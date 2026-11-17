"""Расшифровка реального интервью: согласие, обезличивание, строки (ТР-5.4, С-7).

Расшифровки в тестах — выдуманные строки; настоящих в репозитории нет.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml
from docx import Document

from helpers import claims_of, write_claims
from strateg.project_check import check_project

CONSENT = "письменно в чате, 12.11.2026"
TRANSCRIPT = (
    "Интервьюер: Расскажите о последнем разе, когда вы заказывали обед.\n"
    "Респондент: В пятницу заказывали на пятерых, ждали курьера минут двадцать.\n"
    "\n"
    "Интервьюер: Что вы делали, пока ждали?\n"
    "Респондент: Сели за стол и начали работать дальше, обед ел остывший.\n"
)
WITH_NAME = (
    "Респондент: Меня зовут Анна Петрова, мой телефон +7 (905) 123-45-67, почта anna.test@example.test.\n"
)
SECRETS = ("Анна", "Петрова", "123-45-67", "anna.test")


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def put(demo: Path, name: str, data: bytes | str) -> Path:
    path = demo / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def sources(demo: Path) -> list[dict]:
    return yaml.safe_load((demo / "sources.yaml").read_text(encoding="utf-8"))["sources"] or []


def project_text(demo: Path, skip_tmp: bool = False) -> str:
    parts = []
    for p in sorted(demo.rglob("*")):
        rel = p.relative_to(demo).parts
        if p.is_file() and not (skip_tmp and rel[0] == ".tmp"):
            parts.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def add(cli, name: str, *extra: str):
    return cli("add", name, "--kind", "интервью", *extra)


# --- согласие ----------------------------------------------------------------------------------------


def test_interview_without_consent_is_refused_with_a_hint_and_nothing_is_registered(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    code, _, err = add(cli, "talk.txt")
    assert code == 1 and "--consent" in err and "knowledge/templates/consent.md" in err
    assert "только с отметкой согласия" in err
    assert sources(demo) == [] and (demo / "inputs" / "talk.txt").is_file()
    assert not list((demo / "inputs").glob("*.txt")) or [p.name for p in (demo / "inputs").glob("*")] == [
        "talk.txt"
    ]


def test_interview_with_consent_stores_the_mark_at_the_source(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    code, out, err = add(cli, "talk.txt", "--consent", CONSENT, "--json")
    assert code == 0, err
    assert json.loads(out)["consent"] == CONSENT
    s = sources(demo)[0]
    assert s["kind"] == "интервью" and s["consent"] == CONSENT
    lines = (demo / s["text_path"]).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("[строка 1] Интервьюер:") and lines[2].startswith("[строка 4] Интервьюер:")
    assert check_project(demo) == []


def test_consent_is_shown_in_the_text_output(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    code, out, _ = add(cli, "talk.txt", "--consent", CONSENT)
    assert code == 0 and f"Согласие собеседника: {CONSENT}" in out


def test_repeat_with_consent_returns_the_same_source(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    code, out, _ = add(cli, "talk.txt", "--consent", CONSENT, "--json")
    assert code == 0 and json.loads(out)["duplicate"] is True and len(sources(demo)) == 1


@pytest.mark.parametrize(
    ("consent", "needle"),
    [
        ("письменно", "нет даты"),
        ("   ", "только с отметкой согласия"),
        ("устно, 12.11.2026, Иван Петров", "имя, телефон, почта"),
        ("письменно, 12.11.2026, +7 (905) 123-45-67", "имя, телефон, почта"),
        ("письменно, 12.11.2026, ivan@example.test", "имя, телефон, почта"),
        ("письменно, 12.11.2026 " + "и ещё " * 60, "длиннее"),
    ],
)
def test_bad_consent_mark_is_refused(demo, cli, consent, needle):
    put(demo, "talk.txt", TRANSCRIPT)
    code, _, err = add(cli, "talk.txt", "--consent", consent)
    assert code == 1 and needle in err
    assert sources(demo) == []


def test_consent_with_another_kind_is_a_usage_error(demo, cli):
    put(demo, "note.txt", "Обычная заметка о рынке.")
    code, _, err = cli("add", "note.txt", "--consent", CONSENT)
    assert code == 2 and "--consent относится к расшифровке интервью" in err
    assert sources(demo) == []


def test_consent_cannot_be_combined_with_confirm(demo, cli):
    assert cli("add", "--confirm", "И-1", "--consent", CONSENT)[0] == 2


def test_consent_field_belongs_to_interviews_only(demo, cli):
    put(demo, "note.txt", "Обычная заметка о рынке.")
    assert cli("add", "note.txt")[0] == 0
    path = demo / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["sources"][0]["consent"] = CONSENT
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    assert any("consent бывает только у источника вида «интервью»" in e for e in check_project(demo))


def test_old_project_without_the_field_stays_clean(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    path = demo / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["sources"][0]["consent"]
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    assert check_project(demo) == []


# --- обезличивание -----------------------------------------------------------------------------------


def test_names_phones_and_mail_never_reach_the_project_files(demo, cli, private):
    put(demo, "talk.txt", TRANSCRIPT + WITH_NAME)
    code, out, err = add(cli, "talk.txt", "--consent", CONSENT)
    assert code == 0, err
    s = sources(demo)[0]
    assert {"телефоны", "адреса почты", "имена"} <= set(s["anonymized"])
    text = (demo / s["text_path"]).read_text(encoding="utf-8")
    assert "[имя]" in text and "[телефон]" in text and "[почта]" in text
    assert not (demo / "inputs" / "talk.txt").exists()  # исходник вынесен из inputs/
    pending = demo / ".tmp" / "pending" / s["id"] / "talk.txt"
    assert pending.is_file()
    visible = project_text(demo, skip_tmp=True)
    for secret in SECRETS:
        assert secret not in visible, secret
    for secret in SECRETS:
        assert secret not in out, secret
    # человек подтверждает замены: исходник уходит из проекта совсем
    assert cli("add", "--confirm", s["id"])[0] == 0
    assert not pending.exists() and (private / "demo" / s["id"] / "talk.txt").is_file()
    for secret in SECRETS:
        assert secret not in project_text(demo), secret


def test_unconfirmed_interview_cannot_be_quoted(demo, cli, tmp_path):
    put(demo, "talk.txt", TRANSCRIPT + WITH_NAME)
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [interview_claim()]))
    assert code == 1 and "не подтвердил замены" in err


def test_names_are_replaced_everywhere_in_the_text_not_only_the_first_time(demo, cli):
    put(demo, "talk.txt", "Респондент: Мария заказывает, потом Мария звонит, а Мария ждёт.\n")
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    text = (demo / sources(demo)[0]["text_path"]).read_text(encoding="utf-8")
    assert "Мария" not in text and text.count("[имя]") == 3


# --- строки и цитаты ---------------------------------------------------------------------------------


def interview_claim(locator: str | None = None, text: str = "ждали курьера минут двадцать") -> dict:
    quote = {
        "source": "И-1",
        "text": text,
        "publisher": "собеседник",
        "publisher_kind": "материал пользователя",
        "self_reported": True,
    }
    if locator:
        quote["locator"] = locator
    return {
        "text": "Один респондент рассказал, что ждал курьера около двадцати минут",
        "type": "гипотеза",
        "subject": "ожидание курьера",
        "check_method": "спросить десять офис-менеджеров",
        "quotes": [quote],
    }


@pytest.fixture
def talk(demo, cli):
    put(demo, "talk.txt", TRANSCRIPT)
    assert add(cli, "talk.txt", "--consent", CONSENT)[0] == 0
    return demo


def test_quote_of_a_real_interview_gets_its_line(talk, cli, tmp_path):
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [interview_claim()]))
    assert code == 0, err
    assert claims_of(talk)[0]["quotes"][0]["locator"] == "строка 2"
    assert check_project(talk) == []


def test_wrong_line_in_locator_is_refused_by_add_and_by_check(talk, cli, tmp_path):
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [interview_claim("строка 5")]))
    assert code == 1 and "в locator указана «строка 5»" in err
    assert cli("claim", "add", "--file", write_claims(tmp_path, [interview_claim("строка 2")]))[0] == 0
    path = talk / "claims.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["claims"][0]["quotes"][0]["locator"] = "строка 5"
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    assert any("не совпадает с цитатой" in e for e in check_project(talk))


def test_quote_may_not_cross_a_line_boundary(talk, cli, tmp_path):
    text = (
        "Расскажите о последнем разе, когда вы заказывали обед. Респондент: В пятницу заказывали на пятерых"
    )
    code, _, err = cli("claim", "add", "--file", write_claims(tmp_path, [interview_claim(text=text)]))
    assert code == 1 and err.strip()


def test_docx_transcript_gets_line_marks_too(demo, cli, tmp_path):
    doc = Document()
    doc.add_paragraph("Интервьюер: Что вы делали?")
    doc.add_paragraph("Респондент: Сели за стол и начали работать дальше.")
    buf = io.BytesIO()
    doc.save(buf)
    put(demo, "talk.docx", buf.getvalue())
    code, _, err = add(cli, "talk.docx", "--consent", CONSENT)
    assert code == 0, err
    lines = (demo / sources(demo)[0]["text_path"]).read_text(encoding="utf-8").splitlines()
    assert lines == [
        "[строка 1] Интервьюер: Что вы делали?",
        "[строка 2] Респондент: Сели за стол и начали работать дальше.",
    ]


# --- расшифровка под другим видом --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["расшифровка.txt", "interview-3.md", "transcript.txt"])
def test_transcript_by_name_without_a_kind_is_refused(demo, cli, name):
    put(demo, name, "Просто текст.\nЕщё строка.\n")
    code, _, err = cli("add", name)
    assert code == 1 and "похоже на расшифровку интервью" in err and "--consent" in err
    assert sources(demo) == []


def test_transcript_by_dialogue_lines_without_a_kind_is_refused(demo, cli):
    put(demo, "notes.txt", TRANSCRIPT)
    code, _, err = cli("add", "notes.txt")
    assert code == 1 and "похоже на расшифровку интервью" in err
    assert sources(demo) == []


def test_saying_that_it_is_a_note_lets_the_file_through(demo, cli):
    put(demo, "notes.txt", TRANSCRIPT)
    assert cli("add", "notes.txt", "--kind", "заметка пользователя")[0] == 0
    assert sources(demo)[0]["kind"] == "заметка пользователя"


def test_ordinary_note_is_not_taken_for_an_interview(demo, cli):
    put(demo, "market.txt", "Рынок доставки обедов растёт.\nВопрос цены остаётся открытым.\n")
    assert cli("add", "market.txt")[0] == 0
