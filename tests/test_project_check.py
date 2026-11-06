"""Проверка папки проекта: корректный пример и отрицательные примеры с русскими ошибками.

Ошибки в консоль не печатаются (консоль Windows может быть в cp1251): тесты только сравнивают строки.
Файлы читаются и пишутся явно в UTF-8 с LF.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from strateg.models import CostRecord, JournalEvent, independent_primary_sources
from strateg.project_check import (
    changed_outside_agent,
    check_project,
    total_cost_usd,
    work_minutes,
)

FIXTURE = Path(__file__).parent / "fixtures" / "project_valid"
ENGLISH_FRAGMENTS = ("Field required", "Input should", "Value error", "validation error", "Extra inputs")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    return dest


def edit_yaml(project: Path, name: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = project / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"))


def edit_jsonl(project: Path, name: str, change: Callable[[list[dict[str, Any]]], None]) -> None:
    path = project / name
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    change(rows)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    path.write_bytes(text.encode("utf-8"))


def errors_of(project: Path) -> list[str]:
    errors = check_project(project)
    for e in errors:
        assert re.search("[А-Яа-яЁё]", e), e
        assert not any(frag in e for frag in ENGLISH_FRAGMENTS), e
    return errors


def assert_error(project: Path, *needles: str) -> list[str]:
    """Есть ошибка, в которой встречаются все подстроки (файл, запись, суть)."""
    errors = errors_of(project)
    assert any(all(n in e for n in needles) for e in errors), errors
    return errors


def claim(data: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in data["claims"] if c["id"] == cid)


# --- корректный пример ------------------------------------------------------


def test_valid_project_has_no_errors():
    assert check_project(FIXTURE) == []


def test_valid_project_hashes_in_state_match_files():
    assert changed_outside_agent(FIXTURE) == []


def test_fixture_has_expected_shape():
    sources = yaml.safe_load((FIXTURE / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    claims = yaml.safe_load((FIXTURE / "claims.yaml").read_text(encoding="utf-8"))["claims"]
    assert {s["kind"] for s in sources} == {"страница", "PDF"}
    assert sources[0]["fetch"]["region"] == "RU-MOW" and sources[1]["path"].startswith("inputs/")
    assert {c["type"] for c in claims} == {"допущение", "факт", "гипотеза", "прогноз"}
    assert [c["status"] for c in claims].count("заменено") == 1


def test_unquoted_timestamp_from_yaml_is_accepted(project):
    path = project / "sources.yaml"
    text = path.read_text(encoding="utf-8").replace(
        '"2026-11-06T10:12:00+03:00"', "2026-11-06T10:12:00+03:00"
    )
    path.write_bytes(text.encode("utf-8"))
    assert isinstance(yaml.safe_load(text)["sources"][0]["fetch"]["accessed_at"], dt.datetime)
    assert errors_of(project) == []


def test_project_without_folder():
    assert check_project(FIXTURE / "нет-такой") != []


# --- отрицательные примеры из задания ----------------------------------------


def test_fact_without_quote(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2")["quotes"][0].pop("check"))
    assert_error(project, "claims.yaml", "У-2", "факт без проверенной цитаты")


def test_fact_from_synthetic(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0].update(kind="синтетика"))
    assert_error(project, "claims.yaml", "У-2", "факт из синтетики")


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("D:\\data\\report.pdf", "абсолютный путь с буквой диска"),
        ("/etc/report.pdf", "абсолютный путь (начинается с /)"),
        ("\\\\server\\share\\report.pdf", "сетевой путь"),
        ("../report.pdf", "выходит за папку проекта"),
        ("inputs\\report.pdf", "обратная косая черта"),
    ],
)
def test_absolute_or_escaping_path(project, value, needle):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][1].update(path=value))
    assert_error(project, "sources.yaml", "И-2", "поле path", needle)


def test_absolute_path_in_journal_text(project):
    drive = "E:" + "\\" + "work" + "\\" + "x"
    edit_jsonl(project, "journal.jsonl", lambda rows: rows[7].update(message=f"правка в {drive}"))
    assert_error(project, "journal.jsonl", "строка 8", "абсолютный путь")


def test_replaced_without_replacement_link(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-1").pop("replaced_by"))
    assert_error(project, "claims.yaml", "У-1", "без ссылки на замену")


def test_replacement_with_other_region(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-1").update(region="RU-SPE"))
    assert_error(project, "claims.yaml", "У-1", "предмет, период или регион у них разные")


def test_replacement_needs_back_reference(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2").pop("replaces"))
    assert_error(project, "claims.yaml", "У-1", "обратной ссылки")


def test_fuzzy_check_on_short_quote(project):
    def change(d):
        q = claim(d, "У-4")["quotes"][0]
        q["text"] = "вырастет с 12 до 15–17"
        q["check"]["method"] = "нечёткое"
        q["check"]["similarity"] = 96.0

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "claims.yaml", "У-4", "quotes[0]", "от 40 знаков")


def test_fuzzy_check_on_long_quote_is_fine(project):
    def change(d):
        q = claim(d, "У-4")["quotes"][0]
        q["check"]["method"] = "нечёткое"
        q["check"]["similarity"] = 96.0

    edit_yaml(project, "claims.yaml", change)
    assert errors_of(project) == []


def test_quote_from_missing_source(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2")["quotes"][0].update(source="И-9"))
    assert_error(project, "claims.yaml", "У-2", "несуществующий источник И-9")


# --- другие правила ----------------------------------------------------------


def test_fact_region_must_match_source_region(project):
    def change(d):
        claim(d, "У-2")["region"] = "RU-SPE"
        claim(d, "У-1")["region"] = "RU-SPE"  # замена остаётся согласованной

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "У-2", "регионы должны совпадать")


def test_basis_cycle(project):
    def change(d):
        claim(d, "У-3")["basis"] = ["У-4"]
        claim(d, "У-4")["basis"] = ["У-3"]

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "claims.yaml", "цикл в основаниях", "У-3", "У-4")


def test_basis_to_missing_claim(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-3").update(basis=["У-9"]))
    assert_error(project, "У-3", "поле basis", "несуществующее утверждение У-9")


def test_duplicate_ids(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-4").update(id="У-3"))
    assert_error(project, "claims.yaml", "У-3 встречается больше одного раза")


def test_snapshot_changed_after_registration(project):
    path = project / "sources" / "i-1.txt"
    path.write_bytes(path.read_bytes() + "Добавлено.\n".encode())
    assert_error(project, "sources.yaml", "И-1", "text_sha256", "снимок изменён")
    assert "sources/i-1.txt" in changed_outside_agent(project)


def test_quote_checked_against_another_snapshot(project):
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: claim(d, "У-2")["quotes"][0]["check"].update(snapshot_sha256="0" * 64),
    )
    assert_error(project, "У-2", "другому снимку")


def test_quote_from_source_without_snapshot(project):
    def change(d):
        s = d["sources"][0]
        s["access"] = "нужен браузер"
        for k in ("text_path", "text_sha256"):
            s.pop(k)
        s["fetch"] = {"accessed_at": s["fetch"]["accessed_at"]}

    edit_yaml(project, "sources.yaml", change)
    assert_error(project, "У-2", "нет снимка текста")


def test_locator_required_for_pdf_quote(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-4")["quotes"][0].pop("locator"))
    assert_error(project, "У-4", "нет места (locator)")


def test_suspicious_source_marks_claim(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0].update(suspicious=True))
    assert_error(project, "У-2", "подозрительный")


def test_duplicate_done_key_in_journal(project):
    edit_jsonl(project, "journal.jsonl", lambda rows: rows.append(dict(rows[6])))
    assert_error(project, "journal.jsonl", "строка 10", "повторная отметка выполнения")


def test_duplicate_cost_message(project):
    edit_jsonl(project, "cost.jsonl", lambda rows: rows.append(dict(rows[0])))
    assert_error(project, "cost.jsonl", "строка 5", "msg_01A", "удвоил бы")


def test_instruction_event_refers_to_missing_source(project):
    def change(rows):
        rows.append(
            {
                "schema_version": 1,
                "time": "2026-11-06T10:16:00+03:00",
                "actor": "CLI",
                "kind": "найдена инструкция в источнике",
                "step": "2б",
                "source": "И-7",
            }
        )

    edit_jsonl(project, "journal.jsonl", change)
    assert_error(project, "journal.jsonl", "несуществующий источник И-7")


# --- формат значений и файлов --------------------------------------------------


def test_unknown_field(project):
    edit_yaml(project, "state.yaml", lambda d: d.update(nextaction="x"))
    assert_error(project, "state.yaml", "поле nextaction", "неизвестное поле")


def test_unsupported_schema_version(project):
    edit_yaml(project, "claims.yaml", lambda d: d.update(schema_version=2))
    assert_error(project, "claims.yaml", "версия схемы", "не поддерживается")


def test_unquoted_step_number_explains_quotes(project):
    path = project / "state.yaml"
    path.write_bytes(path.read_bytes().replace(b'step: "2\xd0\xb1"', b"step: 3"))
    assert_error(project, "state.yaml", "поле step", "в кавычки")


def test_yaml_bool_and_date_in_string_fields(project):
    def change(d):
        claim(d, "У-2")["region"] = True  # то, во что YAML превращает no/yes
        claim(d, "У-2")["period"] = dt.date(2026, 11, 6)

    edit_yaml(project, "claims.yaml", change)
    errors = assert_error(project, "У-2", "поле region", "в кавычки")
    assert any("поле period" in e and "в кавычки" in e for e in errors)


def test_time_without_timezone(project):
    edit_jsonl(project, "journal.jsonl", lambda rows: rows[0].update(time="2026-11-06T10:00:00"))
    assert_error(project, "journal.jsonl", "строка 1", "без часового пояса")


def test_cyrillic_latin_lookalike_in_id(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2")["quotes"][0].update(source="I-1"))
    assert_error(project, "У-2", "поле quotes[0].source", "буква русская")


def test_broken_json_line(project):
    path = project / "journal.jsonl"
    path.write_bytes(path.read_bytes() + "{не json}\n".encode())
    assert_error(project, "journal.jsonl", "строка 10", "не JSON")


def test_crlf_and_bom_and_missing_file(project):
    p = project / "claims.yaml"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    s = project / "state.yaml"
    s.write_bytes(b"\xef\xbb\xbf" + s.read_bytes())
    (project / "cost.jsonl").unlink()
    errors = errors_of(project)
    assert "claims.yaml: окончания строк CRLF, нужны LF" in errors
    assert "state.yaml: файл с BOM, нужен UTF-8 без BOM" in errors
    assert "cost.jsonl: файл не найден" in errors


def test_cp1251_file_is_rejected(project):
    (project / "claims.yaml").write_bytes("schema_version: 1\nclaims: []  # проверка\n".encode("cp1251"))
    assert_error(project, "claims.yaml", "не в кодировке UTF-8")


def test_missing_snapshot_file(project):
    (project / "sources" / "i-1.txt").unlink()
    assert_error(project, "sources.yaml", "И-1", "sources/i-1.txt не найден")


def test_original_material_missing(project):
    (project / "inputs" / "report.pdf").unlink()
    assert_error(project, "sources.yaml", "И-2", "поле path", "не найден")


# --- проверка материалов человека -------------------------------------------


def test_anonymized_material_needs_confirmation(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][1].update(anonymized=["телефоны"]))
    assert_error(project, "И-2", "не подтвердил обезличенную копию")


def test_closed_markers_need_confirmation(project):
    marker = ["для служебного пользования"]
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][1].update(closed_markers=marker))
    assert_error(project, "И-2", "признаки закрытых данных")


def test_image_needs_no_personal_data_confirmation(project):
    shutil.copy(project / "inputs" / "report.pdf", project / "inputs" / "screen.png")

    def change(d):
        image = {"id": "И-3", "kind": "изображение", "path": "inputs/screen.png", "access": "получен"}
        d["sources"].append(image)

    edit_yaml(project, "sources.yaml", change)
    assert_error(project, "И-3", "персональных данных на нём нет")
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][2].update(no_personal_data_confirmed=True))
    assert errors_of(project) == []


def test_web_source_needs_fetch_metadata(project):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0]["fetch"].pop("raw_sha256"))
    assert_error(project, "И-1", "в fetch нет: raw_sha256")


# --- вычисляемое ----------------------------------------------------------------


def test_independent_primary_sources_counts_publishers(project):
    from strateg.models import ClaimsFile

    data = yaml.safe_load((FIXTURE / "claims.yaml").read_text(encoding="utf-8"))
    cl = ClaimsFile.model_validate(data).claims[1]
    assert independent_primary_sources(cl) == 1
    second = dict(data["claims"][1]["quotes"][0], publisher="  Компания «ПРИМЕР» ")
    data["claims"][1]["quotes"].append(second)
    assert independent_primary_sources(ClaimsFile.model_validate(data).claims[1]) == 1  # перепечатка
    data["claims"][1]["quotes"][1]["publisher"] = "другой издатель"
    assert independent_primary_sources(ClaimsFile.model_validate(data).claims[1]) == 2


def _jsonl(name: str, model):
    lines = (FIXTURE / name).read_text(encoding="utf-8").splitlines()
    return [model.model_validate_json(line) for line in lines]


def test_work_minutes_sums_done_actions_only():
    events = _jsonl("journal.jsonl", JournalEvent)
    assert work_minutes(events) == pytest.approx((0.2 + 4.1 + 0.3) / 60)


def test_total_cost_takes_session_total_or_sum_of_messages():
    records = _jsonl("cost.jsonl", CostRecord)
    assert total_cost_usd(records) == pytest.approx(0.21 + 0.17 + 0.43)
    assert total_cost_usd([r for r in records if r.cost_usd is None]) is None
