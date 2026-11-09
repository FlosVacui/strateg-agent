"""`strateg fetch`: один источник — один номер, свежий снимок `--refresh` (ADR-0004). Сеть — заглушка."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml

from helpers import URL, edit_yaml, fact, journal_of, load_yaml, open_fixture, rehash, write_claims
from strateg import project as proj
from strateg.hooks import shell
from strateg.project_check import check_project

OTHER = "https://example.com/protection-copy"
NEW_TEXT = (
    "<html><body><article><p>Стоимость подписки в Москве — 249 рублей в месяц. "
    "Первые семь дней бесплатно, дальше списывается абонентская плата. Подписка работает на всех тарифах."
    "</p></article></body></html>"
)


def fetch(cli, *argv: str) -> tuple[int, dict]:
    code, out, err = cli("fetch", *argv, "--json")
    return code, json.loads(out)


def sources(root) -> list[dict]:
    return load_yaml(root / "sources.yaml")["sources"]


def fetch_events(root, **kw) -> list[dict]:
    return [e for e in journal_of(root) if e.get("name") == "strateg fetch"]


@pytest.fixture
def demo(cli, net, page_html, home):
    net.add(URL, page_html)
    assert cli("new", "demo")[0] == 0
    return home / "demo"


# --- Повтор адреса при любом access ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "access"),
    [(500, "источник недоступен: ошибка"), (403, "источник недоступен: защита")],
)
def test_unavailable_address_is_tried_again_and_updates_the_same_record(
    cli, net, page_html, demo, status, access
):
    net.add(URL, status=status)
    code, data = fetch(cli, URL)
    assert code == 1 and data["source"] == "И-1" and data["access"] == access
    assert len(sources(demo)) == 1
    net.add(URL, page_html)
    code, data = fetch(cli, URL)
    assert code == 0 and data["source"] == "И-1" and data["cached"] is False and data["access"] == "получен"
    assert len(sources(demo)) == 1 and sources(demo)[0]["access"] == "получен"
    assert (demo / "sources" / "i-1.txt").is_file()
    code, data = fetch(cli, URL)  # теперь снимок есть: сеть не нужна
    assert code == 0 and data["cached"] is True and data["source"] == "И-1"
    assert check_project(demo) == []


def test_repeat_of_an_unavailable_address_stays_one_record_and_keeps_the_journal_honest(cli, net, demo):
    net.add(URL, status=500)
    for _ in range(3):
        assert fetch(cli, URL)[0] == 1
    assert [s["id"] for s in sources(demo)] == ["И-1"]
    statuses = [e["status"] for e in fetch_events(demo)]
    assert statuses == ["начато", "ошибка"] * 3  # у каждой попытки свой «начато» и итог, ключ один
    assert len({e["idempotency_hash"] for e in fetch_events(demo)}) == 1
    assert check_project(demo) == []


def test_robots_denied_address_is_tried_again_in_place(cli, net, page_html, demo):
    net.add("https://example.com/robots.txt", "User-agent: *\nDisallow: /protection\n", "text/plain")
    code, data = fetch(cli, URL)
    assert code == 1 and data["access"] == "запрещено robots.txt"
    net.add("https://example.com/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
    net.t += 25 * 3600  # кэш robots.txt живёт сутки
    code, data = fetch(cli, URL)
    assert code == 0 and data["source"] == "И-1" and len(sources(demo)) == 1
    assert check_project(demo) == []


def test_fragment_and_case_of_host_do_not_make_a_new_source(cli, net, demo):
    assert fetch(cli, URL)[0] == 0
    code, data = fetch(cli, URL.replace("example.com", "EXAMPLE.com") + "#tariffs")
    assert code == 0 and data["source"] == "И-1" and data["cached"] is True
    assert net.urls().count(URL) == 1


# --- Повтор текста по другому адресу ---------------------------------------------------------------------


def test_same_text_at_another_address_makes_no_new_source(cli, net, page_html, demo):
    net.add(OTHER, page_html)
    first = fetch(cli, URL)[1]
    code, data = fetch(cli, OTHER)
    assert code == 0 and data["source"] == "И-1" and data["same_text_as"] == "И-1"
    assert data["sha256"] == first["sha256"] and data["other_urls"] == [OTHER]
    assert [s["id"] for s in sources(demo)] == ["И-1"] and sources(demo)[0]["other_urls"] == [OTHER]
    assert not (demo / "sources" / "i-2.txt").exists()
    text = cli("fetch", OTHER)[1]  # теперь адрес известен: без сети
    assert "снимок уже сохранён" in text
    assert net.urls().count(OTHER) == 1
    assert check_project(demo) == []
    (done,) = [
        e for e in fetch_events(demo) if e["status"] == "выполнено" and "совпадает" in e.get("message", "")
    ]
    assert "И-1" in done["message"]


def test_text_line_names_the_earlier_source(cli, net, page_html, demo):
    net.add(OTHER, page_html)
    cli("fetch", URL)
    code, out, _ = cli("fetch", OTHER)
    assert code == 0 and "текст совпадает с И-1 (другой адрес)" in out


def test_redirect_to_a_known_page_gives_the_known_source(cli, net, page_html, demo):
    net.add("https://example.com/old", status=301, headers={"location": "/protection"})
    cli("fetch", URL)
    code, data = fetch(cli, "https://example.com/old")
    assert code == 0 and data["source"] == "И-1" and data["same_text_as"] == "И-1"
    assert len(sources(demo)) == 1 and check_project(demo) == []


def test_same_text_in_another_region_is_a_separate_source(cli, net, demo):
    cli("fetch", URL)
    code, data = fetch(cli, URL, "--region", "RU-SPE")
    assert code == 0 and data["source"] == "И-2" and "same_text_as" not in data
    assert len(sources(demo)) == 2


def test_other_text_at_another_address_is_a_new_source(cli, net, demo):
    net.add(OTHER, NEW_TEXT)
    cli("fetch", URL)
    assert fetch(cli, OTHER)[1]["source"] == "И-2"


def test_old_project_with_doubles_opens_and_check_is_clean(cli, net, demo):
    cli("fetch", URL)
    shutil.copy(demo / "sources" / "i-1.txt", demo / "sources" / "i-2.txt")

    def double(d: dict) -> None:
        copy = json.loads(json.dumps(d["sources"][0]))
        copy.update(id="И-2", text_path="sources/i-2.txt")
        d["sources"].append(copy)

    edit_yaml(demo, "sources.yaml", double)
    rehash(demo)
    assert check_project(demo) == []
    code, data = fetch(cli, URL)
    assert code == 0 and data["source"] == "И-1" and data["cached"] is True  # первый из дублей
    assert cli("check", "--project", "demo")[0] == 0


def test_refresh_in_a_project_with_doubles_gives_the_newest_snapshot_afterwards(cli, net, demo):
    cli("fetch", URL)
    shutil.copy(demo / "sources" / "i-1.txt", demo / "sources" / "i-2.txt")

    def double(d: dict) -> None:
        copy = json.loads(json.dumps(d["sources"][0]))
        copy.update(id="И-2", text_path="sources/i-2.txt")
        d["sources"].append(copy)

    edit_yaml(demo, "sources.yaml", double)
    rehash(demo)
    net.add(URL, NEW_TEXT)
    assert fetch(cli, URL, "--refresh")[1]["source"] == "И-3"
    code, data = fetch(cli, URL)
    assert code == 0 and data["source"] == "И-3" and data["cached"] is True
    assert check_project(demo) == []


# --- Свежий снимок --------------------------------------------------------------------------------------


def test_refresh_with_the_same_text_marks_the_recheck_and_makes_no_source(cli, net, demo, clock):
    first = fetch(cli, URL)[1]
    clock.advance(40 * 86400)
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["source"] == "И-1" and data["refresh"] == "same" and data["cached"] is False
    assert net.urls().count(URL) == 2  # запрос ушёл в сеть, минуя снимок проекта
    assert data["sha256"] == first["sha256"] and data["rechecked_at"] == str(clock.value)
    (only,) = sources(demo)
    assert only["rechecked_at"] == clock.value.isoformat()
    assert only["fetch"]["accessed_at"] != only["rechecked_at"]
    assert "перепроверен, текст тот же" in cli("fetch", URL, "--refresh")[1]
    assert check_project(demo) == []


def test_repeated_refreshes_keep_the_journal_clean(cli, net, demo, clock):
    cli("fetch", URL)
    for _ in range(3):
        clock.advance(3600)
        assert fetch(cli, URL, "--refresh")[0] == 0
    done = [e for e in fetch_events(demo) if e["status"] == "выполнено"]
    assert len(done) == 4 and len({e["idempotency_hash"] for e in done}) == 4  # загрузка и три перепроверки
    assert check_project(demo) == []


def test_refresh_keeps_robots_and_the_text_checks_of_a_plain_fetch(cli, net, demo):
    cli("fetch", URL)
    net.add("https://example.com/robots.txt", "User-agent: *\nDisallow: /protection\n", "text/plain")
    net.t += 25 * 3600
    code, data = fetch(cli, URL, "--refresh")
    assert code == 1 and data["refresh"] == "failed" and "запрещено robots.txt" in data["error"]
    assert sources(demo)[0]["access"] == "получен" and check_project(demo) == []
    marked = (
        "<html><body><p>"
        + "Условия подписки описаны подробно. " * 6
        + "</p><p>Коммерческая тайна</p></body></html>"
    )
    net.add("https://example.com/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
    net.add(URL, marked)
    net.t += 25 * 3600
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["refresh"] == "new" and data["closed_markers"] == ["коммерческая тайна"]
    assert sources(demo)[1]["closed_markers"] == ["коммерческая тайна"] and not sources(demo)[1].get(
        "closed_confirmed"
    )
    assert net.sleeps  # пауза к хосту, как у обычной загрузки


def test_refresh_with_other_text_makes_a_new_source_that_points_to_the_old_one(
    cli, net, demo, clock, tmp_path
):
    fetch(cli, URL)
    cli("claim", "add", "--file", write_claims(tmp_path, [fact()]), "--project", "demo")
    old_text = (demo / "sources" / "i-1.txt").read_bytes()
    old_hash = sources(demo)[0]["text_sha256"]
    net.add(URL, NEW_TEXT)
    clock.advance(90 * 86400)
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["source"] == "И-2" and data["refresh"] == "new" and data["refreshes"] == "И-1"
    assert data["claims_on_previous"] == ["У-1"] and data["sha256"] != old_hash
    new, old = sources(demo)[1], sources(demo)[0]
    assert (
        new["refreshes"] == "И-1"
        and new["url"] == URL
        and new["fetch"]["accessed_at"] == clock.value.isoformat()
    )
    assert old.get("rechecked_at") is None and old["text_sha256"] == old_hash
    assert (demo / "sources" / "i-1.txt").read_bytes() == old_text  # прежний снимок цел
    # цитаты по прежнему снимку проверяются как раньше
    assert check_project(demo) == []
    text = cli("fetch", URL, "--refresh")[1]
    assert "перепроверен" in text  # третий запрос: текст тот же, что у И-2
    assert [s["id"] for s in sources(demo)] == ["И-1", "И-2"]


def test_refresh_text_names_the_claims_that_stand_on_the_old_source(cli, net, demo, tmp_path):
    cli("fetch", URL)
    cli("claim", "add", "--file", write_claims(tmp_path, [fact()]), "--project", "demo")
    net.add(URL, NEW_TEXT)
    code, out, _ = cli("fetch", URL, "--refresh")
    assert code == 0
    assert "Источник И-2: текст изменился" in out and "прежний снимок и его цитаты не тронуты" in out
    assert "стоят на И-1: У-1" in out and "claim replace" in out


def test_plain_fetch_after_refresh_returns_the_newest_snapshot(cli, net, demo):
    cli("fetch", URL)
    net.add(URL, NEW_TEXT)
    cli("fetch", URL, "--refresh")
    code, data = fetch(cli, URL)
    assert data["source"] == "И-2" and data["cached"] is True
    net.add(
        URL,
        "<html><body><p>" + "Третья версия страницы с другой ценой в 300 рублей. " * 4 + "</p></body></html>",
    )
    assert fetch(cli, URL, "--refresh")[1]["source"] == "И-3"
    assert [s.get("refreshes") for s in sources(demo)] == [None, "И-1", "И-2"]
    assert check_project(demo) == []


def test_refresh_of_an_unavailable_page_leaves_the_record_as_it_was(cli, net, demo, clock):
    cli("fetch", URL)
    before = (demo / "sources.yaml").read_bytes()
    snapshot = (demo / "sources" / "i-1.txt").read_bytes()
    net.add(URL, status=500)
    clock.advance(3600)
    code, data = fetch(cli, URL, "--refresh")
    assert code == 1 and data["ok"] is False and data["refresh"] == "failed" and data["previous"] == "И-1"
    assert "перепроверить И-1 не удалось" in data["error"] and "не изменены" in data["error"]
    assert (demo / "sources.yaml").read_bytes() == before
    assert (demo / "sources" / "i-1.txt").read_bytes() == snapshot
    code, out, err = cli("fetch", URL, "--refresh")
    assert code == 1 and "перепроверить И-1 не удалось" in err
    last = fetch_events(demo)[-1]
    assert last["status"] == "ошибка" and "не удалась" in last["message"]
    assert check_project(demo) == []
    net.add(URL, open_fixture("tariff.html"))  # страница вернулась: перепроверка проходит
    assert fetch(cli, URL, "--refresh")[1]["refresh"] == "same"


def test_refresh_without_a_snapshot_is_a_plain_fetch(cli, net, demo):
    code, data = fetch(cli, URL, "--refresh")
    assert code == 0 and data["source"] == "И-1" and data["refresh"] == "first" and data["cached"] is False
    assert data["refreshes"] is None and data["claims_on_previous"] == []
    assert len(sources(demo)) == 1 and check_project(demo) == []
    net.add("https://example.com/bad", status=500)
    assert (
        fetch(cli, "https://example.com/bad", "--refresh")[1]["refresh"] == "first"
    )  # недоступный адрес — тоже


def test_refresh_and_plain_fetch_text_without_the_flag_has_no_refresh_fields(cli, net, demo):
    data = fetch(cli, URL)[1]
    assert "refresh" not in data and "claims_on_previous" not in data


def test_refreshes_must_point_to_an_existing_source(cli, net, demo):
    cli("fetch", URL)
    edit_yaml(demo, "sources.yaml", lambda d: d["sources"][0].update(refreshes="И-7"))
    rehash(demo)
    assert any("refreshes" in e and "И-7" in e for e in check_project(demo))


def test_recheck_fields_belong_to_web_sources_only(tmp_path):
    from strateg.models import Source

    base = {
        "id": "И-1",
        "kind": "интервью",
        "path": "inputs/a.txt",
        "access": "получен",
        "text_path": "inputs/a.txt",
        "text_sha256": "a" * 64,
    }
    Source.model_validate(base)
    for extra in ({"rechecked_at": "2026-11-06T10:00:00+03:00"}, {"refreshes": "И-2"}, {"other_urls": [URL]}):
        with pytest.raises(ValueError):
            Source.model_validate({**base, **extra})


# --- Команда исследователя в хуке прав --------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "uv run strateg fetch 'https://example.com/a' --refresh",
        "uv run strateg fetch 'https://example.com/a' --region RU-MOW --refresh",
        "uv run strateg fetch https://example.com/a --refresh",
    ],
)
def test_researcher_may_refresh(command):
    assert shell.fetch_command(command).ok


@pytest.mark.parametrize(
    "command",
    [
        "uv run strateg fetch 'https://example.com/a' --refresh; ls",
        "uv run strateg fetch 'https://example.com/a' --refresh --other",
        "uv run strateg fetch 'https://example.com/a' --refreshx",
    ],
)
def test_researcher_refresh_template_stays_closed_for_anything_else(command):
    assert not shell.fetch_command(command).ok


def test_the_journal_and_state_stay_consistent_after_refresh(cli, net, demo, clock):
    cli("fetch", URL)
    net.add(URL, NEW_TEXT)
    cli("fetch", URL, "--refresh")
    assert proj.Project.load(demo).outside == []
    assert yaml.safe_load((demo / "state.yaml").read_text(encoding="utf-8"))["files"]["sources/i-2.txt"]
