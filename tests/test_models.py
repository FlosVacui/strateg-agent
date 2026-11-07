"""Правила внутри одной записи: модели отвергают нарушения по-русски."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError

from strateg.models import (
    ClaimsFile,
    CostRecord,
    GateEvent,
    JournalEvent,
    ProjectState,
    SourcesFile,
    quote_in_snapshot,
)

SHA = "a" * 64
T = "2026-11-06T10:00:00+03:00"


def messages(model, data: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return "\n".join(str(e.get("ctx", {}).get("error", e["msg"])) for e in exc.value.errors())


def quote(**kw: Any) -> dict[str, Any]:
    q = {
        "source": "И-1",
        "text": "цитата",
        "locator": "стр. 1",
        "publisher": "Издатель",
        "publisher_kind": "СМИ",
        "self_reported": False,
        "check": {"method": "точное вхождение", "similarity": 100, "snapshot_sha256": SHA, "checked_at": T},
    }
    return q | kw


def claim(**kw: Any) -> dict[str, Any]:
    c = {
        "id": "У-1",
        "text": "текст",
        "type": "допущение",
        "origin": "агент",
        "subject": "предмет",
        "taken_from": "рамка",
        "status": "действует",
        "status_history": [{"status": "действует", "at": T, "by": "агент"}],
        "step": "2б",
        "attempt": 1,
    }
    return c | kw


def parse(c: dict[str, Any]):
    return ClaimsFile.model_validate({"schema_version": 1, "claims": [c]})


def test_claim_types_require_their_fields():
    assert "нет оснований" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(type="гипотеза", check_method="м")]}
    )
    assert "нет способа проверки" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(type="гипотеза", basis=["У-2"])]}
    )
    assert "нет диапазона" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(type="прогноз", basis=["У-2"])]}
    )
    assert "наоборот" in messages(
        ClaimsFile,
        {
            "schema_version": 1,
            "claims": [
                claim(type="прогноз", basis=["У-2"], forecast_range={"low": 5, "high": 1, "unit": "%"})
            ],
        },
    )
    assert "откуда оно взято" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(taken_from=None)]}
    )


def test_user_edit_without_source_is_assumption():
    parse(claim(origin="пользователь"))
    text = messages(
        ClaimsFile,
        {
            "schema_version": 1,
            "claims": [claim(type="гипотеза", origin="пользователь", basis=["У-2"], check_method="м")],
        },
    )
    assert "допущение с происхождением «пользователь»" in text


def test_synthetic_is_never_a_fact():
    parse(claim(type="синтетика", origin="синтетика"))
    assert "синтетика не становится другим типом" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(type="синтетика", origin="агент")]}
    )
    text = messages(
        ClaimsFile,
        {"schema_version": 1, "claims": [claim(type="факт", origin="синтетика", quotes=[quote()])]},
    )
    assert "факт не может иметь происхождение «синтетика»" in text


def test_fact_needs_checked_quote_and_publisher_fields():
    fact = claim(type="факт", origin="источник", taken_from=None, quotes=[quote()])
    parse(fact)
    unchecked = fact | {"quotes": [quote(check=None)]}
    assert "факт без проверенной цитаты" in messages(ClaimsFile, {"schema_version": 1, "claims": [unchecked]})
    no_publisher = fact | {"quotes": [quote(publisher=None)]}
    assert "publisher_kind и self_reported" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [no_publisher]}
    )


def test_inexact_method_needs_forty_characters_after_whitespace_normalization():
    check = {"method": "без пробелов и дефисов", "similarity": 100, "snapshot_sha256": SHA, "checked_at": T}
    ok = claim(type="факт", origin="источник", quotes=[quote(text="а" * 40, check=check)])
    parse(ok)
    short = claim(type="факт", origin="источник", quotes=[quote(text="а" * 39, check=check)])
    assert "короче 40 знаков" in messages(ClaimsFile, {"schema_version": 1, "claims": [short]})
    spaced = claim(type="факт", origin="источник", quotes=[quote(text="а" * 39 + "  \n  ", check=check)])
    assert "(в цитате 39)" in messages(ClaimsFile, {"schema_version": 1, "claims": [spaced]})


def test_status_history_rules():
    base = claim()
    revoked = base | {
        "status": "отозвано",
        "status_history": [
            {"status": "действует", "at": T, "by": "агент"},
            {"status": "отозвано", "at": "2026-11-06T11:00:00+03:00", "by": "пользователь"},
        ],
    }
    parse(revoked)  # отзыв — новая запись, старая не стирается
    erased = revoked | {"status_history": [revoked["status_history"][1]]}
    text = messages(ClaimsFile, {"schema_version": 1, "claims": [erased]})
    assert "начинается не с «действует»" in text
    mismatch = base | {"status": "отозвано"}
    assert "не совпадает с последней записью" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [mismatch]}
    )
    back = revoked | {
        "status_history": revoked["status_history"]
        + [revoked["status_history"][0] | {"at": "2026-11-06T12:00:00+03:00"}],
        "status": "действует",
    }
    assert "конечный" in messages(ClaimsFile, {"schema_version": 1, "claims": [back]})


def test_replaced_status_and_link_go_together():
    replaced = claim(
        status="заменено",
        status_history=[
            {"status": "действует", "at": T, "by": "агент"},
            {"status": "заменено", "at": "2026-11-06T11:00:00+03:00", "by": "CLI"},
        ],
    )
    assert "без ссылки на замену" in messages(ClaimsFile, {"schema_version": 1, "claims": [replaced]})
    parse(replaced | {"replaced_by": "У-2"})
    assert "только у «заменено»" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(replaced_by="У-2")]}
    )


def test_journal_event_rules():
    def event(**kw):
        return {"schema_version": 1, "time": T, "actor": "CLI", "kind": "действие", "step": "1"} | kw

    JournalEvent.model_validate(event(name="a", idempotency_hash=SHA, status="выполнено", duration_s=1.5))
    text = messages(JournalEvent, event(name="a", idempotency_hash=SHA, status="выполнено"))
    assert "нет длительности" in text
    assert "нет ключа идемпотентности" in messages(JournalEvent, event(name="a", status="начато"))
    assert "блок gate" in messages(JournalEvent, event(kind="ворота"))
    assert "нет нового режима" in messages(JournalEvent, event(kind="смена режима"))
    assert "нет файла" in messages(JournalEvent, event(kind="изменено вне агента"))
    assert "нет пояснения" in messages(JournalEvent, event(kind="ошибка"))
    assert "бывают только у действия" in messages(
        JournalEvent, event(kind="бюджет", message="m", duration_s=1.0)
    )


def test_gate_event_rules():
    gate = {"gate": "В1", "mode": "пилот", "questions": 2, "opened_at": T}
    GateEvent.model_validate(gate)
    GateEvent.model_validate(gate | {"answered_at": "2026-11-06T10:01:00+03:00", "answer": "ок"})
    assert "нет самого ответа" in messages(GateEvent, gate | {"answered_at": T})
    assert "раньше их открытия" in messages(
        GateEvent, gate | {"answered_at": "2026-11-06T09:00:00+03:00", "answer": "ок"}
    )
    with pytest.raises(ValidationError):
        GateEvent.model_validate(gate | {"gate": "В6"})


def test_cost_record_rules():
    base = {
        "schema_version": 1,
        "time": T,
        "session_id": "s",
        "role": "r",
        "model": "m",
        "environment": "claude-code",
        "entry": "по сообщению",
        "message_id": "msg",
        "cost_usd": 0.1,
        "cost_source": "оценка",
    }
    CostRecord.model_validate(base)
    CostRecord.model_validate(
        base | {"cost_usd": None, "cost_source": "нет данных"}
    )  # стоимость необязательна
    assert "нет message_id" in messages(CostRecord, base | {"message_id": None})
    assert "не бывает message_id" in messages(CostRecord, base | {"entry": "итог сессии"})
    assert "тогда и только тогда" in messages(CostRecord, base | {"cost_usd": None})
    with pytest.raises(ValidationError):
        CostRecord.model_validate(base | {"input_tokens": -1})


def test_state_requires_all_gates_and_allows_no_money():
    state = {
        "schema_version": 1,
        "id": "demo",
        "mode": "автопилот",
        "step": "1",
        "gates": {g: "впереди" for g in ("В1", "В2", "В3", "В4", "В5")},
        "budget": {"limit_minutes": 60, "spent_minutes": 0},
    }
    ProjectState.model_validate(state)  # денег может не быть
    del state["gates"]["В4"]
    assert "нет ворот: В4" in messages(ProjectState, state)


def test_source_rules():
    web = {
        "id": "И-1",
        "kind": "страница",
        "url": "https://example.com/a",
        "access": "запрещено robots.txt",
        "fetch": {"accessed_at": T},
    }
    SourcesFile.model_validate({"schema_version": 1, "sources": [web]})
    both = web | {"path": "inputs/a.txt"}
    assert "ровно один адрес" in messages(SourcesFile, {"schema_version": 1, "sources": [both]})
    no_fetch = {k: v for k, v in web.items() if k != "fetch"}
    assert "нет блока fetch" in messages(SourcesFile, {"schema_version": 1, "sources": [no_fetch]})
    wrong_dir = web | {"access": "получен", "text_path": "inputs/a.txt", "text_sha256": SHA}
    text = messages(SourcesFile, {"schema_version": 1, "sources": [wrong_dir]})
    assert "должен лежать в sources/" in text
    with pytest.raises(ValidationError):
        SourcesFile.model_validate({"schema_version": 1, "sources": [web | {"url": "ftp://example.com"}]})


def test_messages_do_not_depend_on_console_encoding():
    """Сообщения правил — обычные строки Unicode: кодировка консоли на них не влияет."""
    text = messages(
        JournalEvent, {"schema_version": 1, "time": T, "actor": "CLI", "kind": "ошибка", "step": "1"}
    )
    assert re.search("[А-Яа-я]", text)


def test_quote_check_similarity_rules():
    exact = quote()["check"]
    parse(claim(type="факт", origin="источник", taken_from=None, quotes=[quote()]))
    bad_exact = quote(check=exact | {"similarity": 3})
    assert "требует сходства 100" in messages(
        ClaimsFile, {"schema_version": 1, "claims": [claim(quotes=[bad_exact])]}
    )


def test_there_is_no_fuzzy_check_method():
    """Нечёткого пути нет: способа «нечёткое» в записи проверки больше не бывает."""
    fuzzy = quote(text="а" * 50, check=quote()["check"] | {"method": "нечёткое", "similarity": 95})
    text = messages(ClaimsFile, {"schema_version": 1, "claims": [claim(quotes=[fuzzy])]})
    assert "точное вхождение" in text and "без пробелов и дефисов" in text  # допустимые способы


@pytest.mark.parametrize("method", ["без пробелов и дефисов"])
def test_short_quote_allows_only_exact_method(method):
    check = quote()["check"] | {"method": method}
    short = claim(quotes=[quote(text="не рост", check=check)])
    text = messages(ClaimsFile, {"schema_version": 1, "claims": [short]})
    assert "короче 40 знаков" in text and "только точным вхождением" in text
    parse(claim(quotes=[quote(text="не рост")]))  # точное вхождение — можно


def test_quote_matching_follows_adr_normalization():
    snapshot = "Стоимость подписки в Москве — «199» рублей в тече-\nние месяца. ЁЖ"
    assert quote_in_snapshot("точное вхождение", 'стоимость  подписки в москве - "199" рублей', snapshot)
    assert quote_in_snapshot("точное вхождение", "еж", snapshot)  # ё и е, регистр
    assert quote_in_snapshot("точное вхождение", "течение месяца", "тече­ние месяца")  # мягкий перенос
    assert not quote_in_snapshot("точное вхождение", "в течение месяца", snapshot)
    assert quote_in_snapshot("без пробелов и дефисов", "в течение месяца", snapshot)
    assert not quote_in_snapshot("без пробелов и дефисов", "в течение года", snapshot)
    with pytest.raises(ValueError):
        quote_in_snapshot("нечёткое", "а", snapshot)


@pytest.mark.parametrize("text", ["­", " ­ ", ".", "-", "«»"])
def test_quote_without_letters_or_digits_is_rejected(text):
    text_msgs = messages(ClaimsFile, {"schema_version": 1, "claims": [claim(quotes=[quote(text=text)])]})
    assert "нет ни буквы, ни цифры" in text_msgs
    for method in ("точное вхождение", "без пробелов и дефисов"):
        assert not quote_in_snapshot(method, text, "любой текст снимка")


def test_normalization_uses_nfkc():
    snapshot = "Цена — １９９ рублей в месяц"  # полноширинные цифры
    assert quote_in_snapshot("точное вхождение", "цена - 199 рублей", snapshot)


def test_similarity_must_be_a_number():
    check = quote()["check"] | {"similarity": "100"}
    assert messages(ClaimsFile, {"schema_version": 1, "claims": [claim(quotes=[quote(check=check)])]})


def test_interrupted_action_status():
    def event(**kw):
        return {"schema_version": 1, "time": T, "actor": "CLI", "kind": "действие", "step": "2б"} | kw

    started = event(name="fetch", idempotency_hash=SHA, status="начато")
    interrupted = event(
        name="fetch", idempotency_hash=SHA, status="прервано", duration_s=3, message="процесс остановлен"
    )
    for e in (started, interrupted, event(name="fetch", idempotency_hash=SHA, status="прервано")):
        JournalEvent.model_validate(e)
    assert "нет ключа идемпотентности" in messages(JournalEvent, event(name="fetch", status="прервано"))
    assert "бывают только у действия" in messages(
        JournalEvent, event(kind="бюджет", message="m", status="прервано")
    )


def test_claim_has_step_and_attempt():
    parse(claim(step="2а", attempt=3))
    with pytest.raises(ValidationError):
        parse({k: v for k, v in claim().items() if k != "step"})
    with pytest.raises(ValidationError):
        parse(claim(attempt=0))
    with pytest.raises(ValidationError):
        parse(claim(attempt=1.0))
    with pytest.raises(ValidationError):
        parse(claim(step="9"))


@pytest.mark.parametrize("bad", [1730000000, "1730000000", 1730000000.0, True, None, "2026-11-06"])
def test_time_is_iso_string_only(bad):
    event = {"schema_version": 1, "time": bad, "actor": "CLI", "kind": "ошибка", "step": "1", "message": "m"}
    with pytest.raises(ValidationError):
        JournalEvent.model_validate(event)


def test_time_accepts_iso_strings_and_aware_datetimes():
    import datetime as dt

    event = {"schema_version": 1, "actor": "CLI", "kind": "ошибка", "step": "1", "message": "m"}
    for ok in (T, "2026-11-06T10:00:00Z", "2026-11-06T10:00+03:00", dt.datetime(2026, 11, 6, tzinfo=dt.UTC)):
        JournalEvent.model_validate(event | {"time": ok})
    with pytest.raises(ValidationError):
        JournalEvent.model_validate(event | {"time": "2026-11-06T10:00:00"})  # без пояса


@pytest.mark.parametrize("bad", [True, 1.0, "1", 2, None])
def test_schema_version_is_strict_integer(bad):
    state = {
        "schema_version": bad,
        "id": "demo",
        "mode": "автопилот",
        "step": "1",
        "gates": {g: "впереди" for g in ("В1", "В2", "В3", "В4", "В5")},
        "budget": {"limit_minutes": 60, "spent_minutes": 0},
    }
    with pytest.raises(ValidationError):
        ProjectState.model_validate(state)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), "0.1", True])
def test_cost_and_duration_are_finite_numbers(bad):
    base = {
        "schema_version": 1,
        "time": T,
        "session_id": "s",
        "role": "r",
        "model": "m",
        "environment": "claude-code",
        "entry": "итог сессии",
        "cost_source": "оценка",
    }
    with pytest.raises(ValidationError):
        CostRecord.model_validate(base | {"cost_usd": bad})
    CostRecord.model_validate(base | {"cost_usd": 3})  # целое — тоже число
    event = {"schema_version": 1, "time": T, "actor": "CLI", "kind": "действие", "step": "1"}
    with pytest.raises(ValidationError):
        JournalEvent.model_validate(
            event | {"name": "a", "idempotency_hash": SHA, "status": "выполнено", "duration_s": bad}
        )


def test_no_traces_of_the_removed_fuzzy_check_in_docs_of_the_models():
    import inspect
    import json
    from pathlib import Path

    from strateg import models

    assert "Нечёткое" not in (models.quote_in_snapshot.__doc__ or "")
    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "schemas" / "claims.schema.json").read_text(encoding="utf-8"))
    desc = json.dumps(schema, ensure_ascii=False)
    assert "нечёткого пути" not in desc and "нечётк" not in inspect.getsource(models.QuoteCheck)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Несовместимо: способ проверки цитаты «нечёткое» убран" in changelog
