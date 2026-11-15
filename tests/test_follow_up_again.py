"""Спор в `impact`, отметка «выполнено» у обязательства, возраст источника от перепроверки (ADR-0016)."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yaml

from helpers import edit_yaml, load_yaml, rehash
from strateg import release
from strateg.project_check import check_project
from test_follow_up import (  # noqa: F401
    STALE_SOURCE,
    WHAT,
    call,
    due,
    fu,
    ids,
    impact,
    keys,
)
from test_release_diff import fu as fu7  # noqa: F401

ZONE = dt.timezone(dt.timedelta(hours=3))


# --- impact: спор -----------------------------------------------------------------------------------------


def dispute_u7_with_new_page(cli, fu: Path, tmp_path: Path) -> tuple[str, str]:
    """Новая страница даёт утверждение со спором к У-7 (на нём Вар-3, О-3 и Реш-1): (источник, утверждение).

    Предмет тот же, регион другой: это не замена, а спор; прежнее У-7 остаётся в силе.
    """
    (fu / "inputs" / "other.txt").write_text(
        "Тарифы «Щита» в Петербурге\n\nКонкурент «Щит» продаёт защиту от мошенников за 59 рублей в месяц.\n",
        encoding="utf-8",
    )
    code, added, err = call(cli, "add", "inputs/other.txt")
    assert code == 0, err
    source = added["source"]
    claim = {
        "text": "Конкурент «Щит» в Петербурге продаёт защиту за 59 рублей в месяц",
        "type": "факт",
        "subject": "цена защиты у конкурента «Щит»",
        "period": "2026",
        "region": "RU-SPE",
        "dispute": {"claim": "У-7", "reason": "в Петербурге цена другая, источники спорят"},
        "quotes": [
            {
                "source": source,
                "text": "Конкурент «Щит» продаёт защиту от мошенников за 59 рублей в месяц",
                "publisher": "компания «Щит»",
                "publisher_kind": "официальный первоисточник",
                "self_reported": False,
            }
        ],
    }
    path = tmp_path / "dispute7.yaml"
    path.write_text(yaml.safe_dump(claim, allow_unicode=True), encoding="utf-8")
    code, added_claim, err = call(cli, "claim", "add", "--file", str(path))
    assert code == 0, err
    return source, added_claim["claims"][0]["id"]


def test_a_new_claim_with_a_dispute_reaches_the_disputed_one_and_what_stands_on_it(cli, fu, tmp_path):
    source, new = dispute_u7_with_new_page(cli, fu, tmp_path)
    r = impact(cli, new)
    assert {e["id"]: e["why"] for e in r["claims"]} == {"У-7": [f"оспаривается {new}"]}
    assert (
        ids(r["options"]) == ["Вар-3"] and ids(r["promises"]) == ["О-3"] and ids(r["decisions"]) == ["Реш-1"]
    )
    assert "основание У-7" in r["decisions"][0]["why"]
    assert r["choice"]["affected"] is True and "решение Реш-1 опирается на У-7" in r["choice"]["via"]
    assert r["choice"]["stop"] == "внеплановая остановка: спросить человека до пересчёта"
    # со стороны оспоренного: на него пришло новое (так было и раньше)
    old = impact(cli, "У-7")
    assert {e["id"]: e["why"] for e in old["claims"]} == {new: ["спор с У-7"]}
    assert old["choice"]["affected"] is True
    # источник нового утверждения доходит до того же
    from_source = impact(cli, source)
    assert ids(from_source["claims"]) == ["У-7", new] and from_source["choice"]["affected"] is True
    code, out, _ = cli("impact", new, "--project", "fu")
    assert code == 0 and f"У-7 (оспаривается {new})" in out and "Выбор на В4: задет" in out


def test_a_dispute_goes_down_to_whatever_stands_on_the_disputed_claim(cli, fu):
    """У-6 стоит на У-3; утверждение со спором к У-3 задевает и У-6, и варианты с возражениями на У-3."""
    edit_yaml(
        fu,
        "claims.yaml",
        lambda d: next(c for c in d["claims"] if c["id"] == "У-8").update(
            dispute={"claim": "У-3", "reason": "допущение оспорено"}
        ),
    )
    rehash(fu)
    r = impact(cli, "У-8")
    why = {e["id"]: e["why"] for e in r["claims"]}
    assert why["У-3"] == ["оспаривается У-8"] and why["У-6"] == ["основание У-3"] and "У-9" in why
    assert {"Вар-1", "Вар-2", "Вар-3"} <= set(ids(r["options"]))
    assert r["choice"]["affected"] is True  # допущение решения У-3 задето через спор


def test_a_claim_without_a_dispute_does_not_reach_anything_extra(cli, fu):
    r = impact(cli, "У-9")
    assert {e["id"]: e["why"] for e in r["claims"]} == {"У-8": ["заменено на У-9"]}


# --- Обязательства: «выполнено» ---------------------------------------------------------------------------


def mark_done(fu: Path, done: str | None = "2026-11-18", note: str | None = None) -> None:
    def put(d: dict) -> None:
        cm = d["decisions"][0]["commitments"][0]
        if done is not None:
            cm["done"] = done
        if note is not None:
            cm["done_note"] = note

    edit_yaml(fu, "decisions.yaml", put)
    rehash(fu)


def test_a_done_commitment_leaves_due(cli, fu):
    assert ("commitments", "Реш-1", WHAT) in keys(due(cli, "2026-11-25"))
    mark_done(fu, note="выгрузку прислали")
    assert check_project(fu) == []
    for today in ("2026-11-13", "2026-11-25", "2027-01-20"):
        assert not due(cli, today)["groups"]["commitments"], today


def test_a_done_commitment_does_not_bring_the_window_event_back(cli, fu):
    def fresh(d: dict) -> None:
        for src in d["sources"]:
            if "fetch" in src:
                src["fetch"]["accessed_at"] = "2026-11-01T09:00:00+03:00"

    edit_yaml(fu, "sources.yaml", fresh)
    rehash(fu)
    first = due(cli, "2026-11-02")
    assert first["empty"] and first["next_event"]["date"] == "2026-11-13"
    assert "подходит срок обязательства" in first["next_event"]["text"]
    mark_done(fu)
    later = due(cli, "2026-11-02")["next_event"]
    assert later["date"] != "2026-11-13" and "обязательства" not in later["text"]


def test_a_done_commitment_does_not_hide_the_others(cli, fu):
    def two(d: dict) -> None:
        d["decisions"][0]["commitments"].append(
            {"what": "согласовать пороги с финансами", "due": "2026-11-20", "owner": "финансовый директор"}
        )

    edit_yaml(fu, "decisions.yaml", two)
    mark_done(fu)
    items = due(cli, "2026-11-25")["groups"]["commitments"]
    assert [i["what"] for i in items] == ["согласовать пороги с финансами"]


def test_check_takes_the_done_date_in_the_project_time_zone(cli, fu, clock):
    clock.value = dt.datetime(2026, 11, 18, 23, 30, tzinfo=ZONE)
    mark_done(fu, "2026-11-18")
    assert check_project(fu) == []  # сегодня в поясе проекта — 18 ноября
    mark_done(fu, "2026-11-19")
    errors = check_project(fu)
    assert len(errors) == 1 and "commitments[0].done" in errors[0] and "в будущем" in errors[0]
    assert cli("check", "--project", "fu")[0] == 1
    clock.value = dt.datetime(2026, 11, 19, 0, 5, tzinfo=ZONE)
    assert check_project(fu) == []


def test_done_note_without_a_date_is_refused(cli, fu):
    mark_done(fu, None, note="сказали, что готово")
    errors = check_project(fu)
    assert errors and any("done_note" in e for e in errors)


def test_decisions_without_the_mark_stay_as_they_were(cli, fu):
    assert "done" not in json.dumps(load_yaml(fu / "decisions.yaml"))
    assert check_project(fu) == []


def write_decisions(cli, fu: Path, mutate) -> tuple[int, dict | None, str]:
    data = load_yaml(fu / "decisions.yaml")
    data.pop("schema_version")
    mutate(data)
    code, out, err = cli(
        "write", "--file", "-", "--project", "fu", "--by", "пользователь", "--json",
        stdin=json.dumps({"decisions": data}, ensure_ascii=False),
    )  # fmt: skip
    return code, json.loads(out) if out.strip() else None, err


def test_write_records_done_after_the_release_and_still_guards_the_released_decision(cli, fu7):
    fu = fu7
    assert cli("step", "done", "7", "--project", "fu")[0] == 0
    assert release.released_decisions(fu) is not None
    code, out, err = write_decisions(
        cli,
        fu,
        lambda d: d["decisions"][0]["commitments"][0].update(done="2026-11-18", done_note="прислали в чате"),
    )
    assert code == 0, (out, err)  # защита решения выпуска от удаления отметку не блокирует
    saved = load_yaml(fu / "decisions.yaml")["decisions"][0]["commitments"][0]
    assert saved["done"] == "2026-11-18" and saved["done_note"] == "прислали в чате"
    assert not due(cli, "2026-11-25")["groups"]["commitments"]
    assert check_project(fu) == []
    code, out, err = write_decisions(cli, fu, lambda d: d["decisions"].clear())
    assert code == 1  # само решение из снимка выпуска убрать по-прежнему нельзя


def test_write_refuses_a_done_date_in_the_future(cli, fu):
    code, out, err = write_decisions(
        cli, fu, lambda d: d["decisions"][0]["commitments"][0].update(done="2027-02-02")
    )
    assert code == 1 and "в будущем" in json.dumps(out, ensure_ascii=False)
    assert "done" not in load_yaml(fu / "decisions.yaml")["decisions"][0]["commitments"][0]


# --- Возраст источника от последней перепроверки ----------------------------------------------------------


def recheck(fu: Path, sid: str, at: str) -> None:
    edit_yaml(
        fu,
        "sources.yaml",
        lambda d: next(s for s in d["sources"] if s["id"] == sid).update(rechecked_at=at),
    )
    rehash(fu)
    assert check_project(fu) == []


def test_due_counts_the_age_of_a_source_from_the_last_recheck(cli, fu):
    assert STALE_SOURCE in keys(due(cli, "2026-09-01"))  # получен 2026-03-01: 184 дня
    recheck(fu, "И-3", "2026-08-20T10:00:00+03:00")
    assert STALE_SOURCE not in keys(due(cli, "2026-09-01"))
    item = next(i for i in due(cli, "2027-02-20")["groups"]["stale"] if i["source"] == "И-3")
    assert item["accessed"] == "2026-08-20" and item["age_days"] == 184
    assert "перепроверенном 2026-08-20" in item["text"]
    # ближайшее событие считается от перепроверки, а не от получения (там было бы 2026-08-29)
    assert due(cli, "2026-08-21")["next_event"]["date"] != "2026-08-29"


def test_a_recheck_older_than_the_receipt_does_not_make_the_source_younger(cli, fu):
    recheck(fu, "И-3", "2026-01-01T10:00:00+03:00")
    assert STALE_SOURCE in keys(due(cli, "2026-09-01"))
    item = next(i for i in due(cli, "2026-09-01")["groups"]["stale"] if i["source"] == "И-3")
    assert (
        item["accessed"] == "2026-03-01" and item["age_days"] == 184
    )  # день получения, не старая перепроверка
    assert "полученном 2026-03-01" in item["text"] and "перепроверенном" not in item["text"]
    assert due(cli, "2026-08-01")["next_event"]["date"] == "2026-08-29"


def add_refreshing_source(fu: Path, accessed: str, rechecked: str | None = None) -> None:
    """Источник И-6 перепроверяет И-3 (новый снимок страницы); утверждения остаются на И-3."""

    def put(d: dict) -> None:
        new = json.loads(json.dumps(next(s for s in d["sources"] if s["id"] == "И-3")))
        new.update(id="И-6", refreshes="И-3", text_path="sources/i-6.txt")
        new["fetch"]["accessed_at"] = accessed
        if rechecked:
            new["rechecked_at"] = rechecked
        d["sources"].append(new)

    (fu / "sources" / "i-6.txt").write_bytes((fu / "sources" / "i-3.txt").read_bytes())
    edit_yaml(fu, "sources.yaml", put)
    rehash(fu)
    assert check_project(fu) == []


def test_a_newer_snapshot_in_the_chain_of_rechecks_keeps_due_quiet_about_the_old_one(cli, fu):
    assert STALE_SOURCE in keys(due(cli, "2026-09-01"))
    add_refreshing_source(fu, "2026-08-20T10:00:00+03:00")  # утверждения всё ещё на И-3
    data = due(cli, "2026-09-01")
    assert STALE_SOURCE not in keys(data)
    stale = [i for i in due(cli, "2027-02-20")["groups"]["stale"] if i["source"] == "И-3"]
    assert stale and stale[0]["accessed"] == "2026-08-20" and "перепроверенном 2026-08-20" in stale[0]["text"]


def test_a_recheck_of_the_newer_snapshot_counts_for_the_whole_chain(cli, fu):
    add_refreshing_source(fu, "2026-05-01T10:00:00+03:00", rechecked="2026-08-25T10:00:00+03:00")
    assert STALE_SOURCE not in keys(due(cli, "2026-09-01"))
    assert STALE_SOURCE in keys(due(cli, "2027-02-27"))  # 186 дней от 2026-08-25
