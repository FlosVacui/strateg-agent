"""Проверка папки проекта: корректный пример и отрицательные примеры с русскими ошибками.

Ошибки в консоль не печатаются (консоль Windows может быть в cp1251): тесты только сравнивают строки.
Файлы читаются и пишутся явно в UTF-8 с LF.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from strateg.models import independent_primary_sources
from strateg.project_check import check_project

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


# --- корректный пример ----------------------------


def test_valid_project_has_no_errors():
    assert check_project(FIXTURE) == []


def test_fixture_hashes_in_state_match_files():
    """Пример остаётся согласованным: sha256 в state.yaml равны хешам файлов."""
    state = yaml.safe_load((FIXTURE / "state.yaml").read_text(encoding="utf-8"))
    for rel, digest in state["files"].items():
        assert hashlib.sha256((FIXTURE / rel).read_bytes()).hexdigest() == digest, rel


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


# --- отрицательные примеры ----------------------------


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
    assert_error(project, "claims.yaml", "У-4", "quotes[0]", "короче 40 знаков")


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


# --- другие правила ----------------------------


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


# --- формат значений и файлов ----------------------------


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


# --- проверка материалов человека ----------------------------


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


# --- вычисляемое ----------------------------


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


# --- проверка цитаты ----------------------------


def _set_quote(d: dict[str, Any], cid: str = "У-2", **fields: Any) -> None:
    check = fields.pop("check", {})
    quote = claim(d, cid)["quotes"][0]
    quote.update(fields)
    quote["check"].update(check)


@pytest.mark.parametrize("method", ["без пробелов и дефисов", "нечёткое"])
def test_short_quote_passes_only_by_exact_match(project, method):
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: _set_quote(d, text="не рост", check={"method": method, "similarity": 100}),
    )
    assert_error(project, "claims.yaml", "У-2", "quotes[0]", "короче 40 знаков", "только точным вхождением")


def test_short_quote_by_exact_match_is_fine(project):
    edit_yaml(project, "claims.yaml", lambda d: _set_quote(d, text="199 рублей в месяц"))
    assert errors_of(project) == []


def test_quote_absent_from_snapshot_is_rejected(project):
    edit_yaml(project, "claims.yaml", lambda d: _set_quote(d, text="Подписка бесплатна навсегда"))
    assert_error(project, "claims.yaml", "У-2", "quotes[0]", "не найдена в снимке И-1", "точное вхождение")


def test_exact_method_requires_similarity_100(project):
    edit_yaml(project, "claims.yaml", lambda d: _set_quote(d, check={"similarity": 3}))
    assert_error(project, "claims.yaml", "У-2", "quotes[0].check", "требует сходства 100")


def test_fuzzy_method_requires_threshold(project):
    def change(d):
        _set_quote(d, "У-4", check={"method": "нечёткое", "similarity": 40})

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "claims.yaml", "У-4", "ниже порога 93")


def test_fuzzy_similarity_is_not_recomputed_here(project):
    """Пересчёт нечёткого сходства — в `quote check`: проект проверяет только порог в записи."""

    def change(d):
        _set_quote(d, "У-4", text="в течение года вырастет с 12 до 15–17 процентов, это опечатка!")
        _set_quote(d, "У-4", check={"method": "нечёткое", "similarity": 95})

    edit_yaml(project, "claims.yaml", change)
    assert errors_of(project) == []


def test_exact_method_does_not_accept_text_found_only_without_spaces(project):
    # В снимке «тече- ние», так что вхождение есть только вида «без пробелов и дефисов».
    edit_yaml(project, "claims.yaml", lambda d: _set_quote(d, "У-4", check={"method": "точное вхождение"}))
    assert_error(project, "У-4", "не найдена в снимке И-2", "«точное вхождение»")


def test_spaceless_method_text_must_be_in_snapshot(project):
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: _set_quote(d, "У-4", text="в течение года вырастет с 12 до 25–27 процентов"),
    )
    assert_error(project, "У-4", "не найдена в снимке И-2", "«без пробелов и дефисов»")


def test_quote_is_matched_after_normalization(project):
    """Регистр, пробелы, неразрывный пробел, тире, кавычки и ё не мешают: нормализация как в ADR."""
    variant = "СТОИМОСТЬ  подписки\u00a0в Москве - 199 рублей в месяц"
    edit_yaml(project, "claims.yaml", lambda d: _set_quote(d, text=variant))
    assert errors_of(project) == []


def test_snapshot_not_searched_when_it_was_changed(project):
    """Изменённый снимок даёт одну ошибку про хеш, а не лавину ошибок про вхождение."""
    path = project / "sources" / "i-1.txt"
    path.write_bytes(path.read_bytes() + "Добавлено.\n".encode())
    errors = errors_of(project)
    assert not any("не найдена в снимке" in e for e in errors)


# --- прерванное действие ----------------------------


def test_interrupted_action_then_repeat_with_same_key(project):
    key = "ab" * 32

    def action(status: str, time: str, **extra: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "time": time,
            "actor": "CLI",
            "kind": "действие",
            "step": "2б",
            "name": "strateg fetch",
            "idempotency_hash": key,
            "status": status,
        } | extra

    def change(rows):
        rows += [
            action("начато", "2026-11-06T10:20:00+03:00"),
            action("прервано", "2026-11-06T10:25:00+03:00", message="процесс остановлен", duration_s=3),
            action("начато", "2026-11-06T10:26:00+03:00"),
            action("выполнено", "2026-11-06T10:26:05+03:00", duration_s=5),
        ]

    edit_jsonl(project, "journal.jsonl", change)
    assert errors_of(project) == []


# --- пути только в полях путей ----------------------------


def test_paths_in_free_text_are_not_errors(project):
    win = "C:" + "\\" + "Users" + "\\" + "Ivan" + "\\" + "prices.xlsx"

    def change(d):
        claim(d, "У-3")["text"] = "Тариф A:/B самый популярный"
        claim(d, "У-3")["check_method"] = "посмотреть каталог /home/user и D:/data"
        claim(d, "У-1")["taken_from"] = f"файл {win}"

    edit_yaml(project, "claims.yaml", change)
    edit_jsonl(project, "journal.jsonl", lambda rows: rows[7].update(message=f"У-1 неверно, см. файл {win}"))
    assert errors_of(project) == []


def test_absolute_path_in_journal_path_field_is_error(project):
    def change(rows):
        rows.append(
            {
                "schema_version": 1,
                "time": "2026-11-06T10:16:00+03:00",
                "actor": "CLI",
                "kind": "изменено вне агента",
                "step": "2б",
                "path": "D:/data/claims.yaml",
            }
        )

    edit_jsonl(project, "journal.jsonl", change)
    assert_error(project, "journal.jsonl", "строка 10", "поле path", "абсолютный путь")


def test_state_files_key_must_be_relative(project):
    edit_yaml(project, "state.yaml", lambda d: d["files"].update({"../secret": "0" * 64}))
    assert_error(project, "state.yaml", "files", "(ключ)", "выходит за папку проекта")


# --- шаг и попытка ----------------------------


def test_fixture_claims_carry_step_and_attempt():
    claims = yaml.safe_load((FIXTURE / "claims.yaml").read_text(encoding="utf-8"))["claims"]
    assert all(c["step"] and c["attempt"] >= 1 for c in claims)


def test_claim_without_step_or_with_zero_attempt(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2").pop("step"))
    assert_error(project, "claims.yaml", "У-2", "поле step", "нет обязательного поля")
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-2").update(step="2б", attempt=0))
    assert_error(project, "claims.yaml", "У-2", "поле attempt", "вне допустимых границ")


# --- замена по кругу ----------------------------


def test_replacement_cycle(project):
    def change(d):
        c1, c2 = claim(d, "У-1"), claim(d, "У-2")
        c1["replaces"] = "У-2"
        c2["replaced_by"] = "У-1"
        c2["status"] = "заменено"
        c2["status_history"].append(
            {"status": "заменено", "at": "2026-11-06T10:30:00+03:00", "by": "CLI", "reason": "по кругу"}
        )

    edit_yaml(project, "claims.yaml", change)
    assert_error(project, "claims.yaml", "цикл замены", "У-1 -> У-2 -> У-1")


# --- строгие типы ----------------------------


@pytest.mark.parametrize("bad", [1730000000, "1730000000", 1730000000.5, True, "2026-11-06"])
def test_time_must_be_iso_string(project, bad):
    edit_jsonl(project, "journal.jsonl", lambda rows: rows[0].update(time=bad))
    assert_error(project, "journal.jsonl", "строка 1", "время должно быть строкой ISO 8601 с поясом")


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_schema_version_must_be_integer(project, bad):
    edit_yaml(project, "state.yaml", lambda d: d.update(schema_version=bad))
    assert_error(project, "state.yaml", "версия схемы", "не поддерживается")


def test_non_finite_cost_is_rejected(project):
    edit_jsonl(project, "cost.jsonl", lambda rows: rows[0].update(cost_usd=float("inf")))
    assert "Infinity" in (project / "cost.jsonl").read_text(encoding="utf-8")
    assert_error(project, "cost.jsonl", "строка 1", "cost_usd", "конечное число")


@pytest.mark.parametrize("bad", ["yes", 1, "true"])
def test_flags_are_booleans_only(project, bad):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][0].update(untrusted=bad))
    assert_error(project, "И-1", "поле untrusted", "логическое значение")


def test_counts_are_integers_only(project):
    edit_jsonl(project, "cost.jsonl", lambda rows: rows[0].update(input_tokens="1200"))
    assert_error(project, "cost.jsonl", "строка 1", "input_tokens", "целое число")


# --- сообщения без имён типов Python ----------------------------

PYTHON_WORDS = re.compile(r"\b(NoneType|None|True|False|list|dict|bool|float|int|str)\b|\[key\]")


@pytest.mark.parametrize("bad", [None, [], {}, True, 1.5, 7])
def test_messages_have_no_python_type_names(project, bad):
    def change(d):
        claim(d, "У-1")["id"] = bad
        claim(d, "У-2")["subject"] = bad
        claim(d, "У-2")["region"] = bad if bad is not None else "RU-MOW"

    edit_yaml(project, "claims.yaml", change)
    errors = errors_of(project)
    assert errors
    assert not [e for e in errors if PYTHON_WORDS.search(e)], errors


def test_record_label_uses_id_only_when_it_is_valid(project):
    edit_yaml(project, "claims.yaml", lambda d: claim(d, "У-1").update(id=True))
    errors = errors_of(project)
    assert any(e.startswith("claims.yaml, запись №1, поле id") for e in errors), errors
    assert not any("запись True" in e for e in errors)


def test_dictionary_key_error_has_no_service_key(project):
    edit_yaml(project, "state.yaml", lambda d: d["gates"].update({"B1": "впереди"}))
    errors = assert_error(project, "state.yaml", "gates", "(ключ)")
    assert not any("[key]" in e for e in errors)


# --- путь в одном виде ----------------------------


@pytest.mark.parametrize(
    "value",
    [
        "inputs//report.pdf",
        "inputs/./report.pdf",
        "inputs/report.pdf ",
        "inputs/report.pdf/",
        "inputs/report.pdf.",
        " inputs/report.pdf",
        "inputs/ report.pdf",
        "./inputs/report.pdf",
    ],
)
def test_path_has_only_canonical_form(project, value):
    edit_yaml(project, "sources.yaml", lambda d: d["sources"][1].update(path=value))
    assert_error(project, "sources.yaml", "И-2", "поле path")


# --- JSONL и YAML ----------------------------


@pytest.mark.parametrize("name", ["journal.jsonl", "cost.jsonl"])
def test_jsonl_needs_final_newline(project, name):
    path = project / name
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    assert_error(project, name, "нет завершающего перевода строки")


def test_duplicate_yaml_key_is_an_error(project):
    path = project / "claims.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count("    type: гипотеза\n") == 1
    text = text.replace("    type: гипотеза\n", "    type: гипотеза\n    type: допущение\n")
    line = text.split("\n").index("    type: допущение") + 1
    path.write_bytes(text.encode("utf-8"))
    assert_error(project, "claims.yaml", "повторяющийся ключ type", f"в строке {line}")


def test_same_key_in_different_records_is_fine(project):
    assert errors_of(project) == []  # в примере у каждого утверждения свой type, id и т. д.


def _chain(n: int, tail: str) -> list[dict[str, Any]]:
    base = yaml.safe_load((FIXTURE / "claims.yaml").read_text(encoding="utf-8"))["claims"][2]
    out = []
    for i in range(n):
        c = dict(base, id=f"У-{i + 10}")
        c["basis"] = [f"У-{i + 11}"] if i + 1 < n else [tail]
        out.append(c)
    return out


def test_long_basis_chain_has_no_recursion_error(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"].extend(_chain(2000, "У-2")))
    assert errors_of(project) == []


def test_long_basis_cycle_is_reported(project):
    edit_yaml(project, "claims.yaml", lambda d: d["claims"].extend(_chain(2000, "У-10")))
    assert_error(project, "claims.yaml", "цикл в основаниях", "У-10")


# --- лишнего нет ----------------------------


def test_computed_helpers_for_cli_are_not_in_this_package():
    from strateg import project_check

    for name in ("work_minutes", "total_cost_usd", "changed_outside_agent"):
        assert not hasattr(project_check, name)
