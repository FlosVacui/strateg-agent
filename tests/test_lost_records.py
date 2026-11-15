"""Потерянная запись видна `check`, а повтор «выполнено» от гонки не держит проект в ошибке навсегда.

Проекты собираются обычными командами с сетью-заглушкой, затем портятся так, как портит гонка: запись об
источнике пропала, а файл остался; в журнале появилась вторая отметка «выполнено» с тем же ключом.
"""

from __future__ import annotations

import copy
import json
import shutil

import pytest

from helpers import URL, edit_journal, edit_yaml, journal_of, rehash
from strateg import project as proj
from strateg.project_check import check_project

OTHER = "https://example.com/tariff-two"


@pytest.fixture
def demo(cli, net, page_html, home):
    """Проект с двумя источниками из сети (И-1, И-2) и материалом из `inputs/` (И-3)."""
    net.add(URL, page_html)
    net.add(OTHER, page_html.replace("199", "249"))
    assert cli("new", "demo")[0] == 0
    root = home / "demo"
    assert cli("fetch", URL)[0] == 0
    assert cli("fetch", OTHER)[0] == 0
    note = root / "inputs" / "заметка.txt"
    note.write_text("Клиенты просят защиту детей, подписка стоит 199 рублей в месяц.", encoding="utf-8")
    assert cli("add", str(note))[0] == 0
    assert check_project(root) == []
    return root


def drop_source(root, sid: str) -> None:
    """Запись об источнике пропала (её затёрла соседняя команда), файлы остались."""
    edit_yaml(root, "sources.yaml", lambda d: d.update(sources=[s for s in d["sources"] if s["id"] != sid]))
    rehash(root)


def repeat_done(root, name: str, index: int = 0) -> None:
    """Вторая отметка «выполнено» с тем же ключом: так её оставляла гонка."""

    def add(events):
        done = [e for e in events if e.get("name") == name and e.get("status") == "выполнено"]
        events.append(copy.deepcopy(done[index]))

    edit_journal(root, add)
    rehash(root)


# --- файлы без записи -------------------------------------------------------------------------------------


def test_snapshot_without_a_record_is_an_error_of_check(cli, demo):
    drop_source(demo, "И-2")
    errors = check_project(demo)
    assert any("sources/i-2.txt" in e and "не ссылается ни одна запись" in e for e in errors)
    code, _, err = cli("check")
    assert code == 1 and "sources/i-2.txt" in err


def test_files_in_inputs_are_not_searched_for_losses(demo):
    """В `inputs/` лежат файлы человека: рядом с исходником бывает его ручная расшифровка, а заметку можно
    назвать как угодно. Запись `add` не теряется (блокировка папки), поэтому check их не трогает."""
    drop_source(demo, "И-3")
    (demo / "inputs" / "отчёт.pdf").write_bytes(b"scan")
    (demo / "inputs" / "отчёт.pdf.txt").write_text("расшифровка скана, сделанная вручную", encoding="utf-8")
    (demo / "inputs" / "i-5.txt").write_text("заметка, названная как служебный текст", encoding="utf-8")
    assert check_project(demo) == []


def test_only_snapshot_names_count_in_sources_and_os_junk_does_not(demo):
    for name in ("Thumbs.db", "desktop.ini", ".DS_Store", "i-9.txt.123.tmp", "заметка.txt"):
        (demo / "sources" / name).write_text("x", encoding="utf-8")
    assert check_project(demo) == []
    (demo / "sources" / "i-9.txt").write_text("лишний снимок", encoding="utf-8")
    assert any("sources/i-9.txt" in e for e in check_project(demo))


def test_service_and_temporary_files_are_not_reported(demo):
    (demo / "sources" / ".hidden").write_text("x", encoding="utf-8")
    (demo / ".tmp" / "pending").mkdir(parents=True, exist_ok=True)
    (demo / ".tmp" / "pending" / "x.txt").write_text("x", encoding="utf-8")
    (demo / "inputs" / ".backup").mkdir(exist_ok=True)
    (demo / "inputs" / ".backup" / "заметка.txt.txt").write_text("x", encoding="utf-8")
    (demo / "inputs" / "лежит.docx").write_bytes(b"not added yet")  # материал человека до `add`
    assert check_project(demo) == []


def test_a_file_the_command_wrote_and_took_back_leaves_nothing(cli, net, demo):
    net.add("https://example.com/broken", "<html><body>коротко</body></html>")
    cli("fetch", "https://example.com/broken")
    assert check_project(demo) == []
    assert sorted(p.name for p in (demo / "sources").iterdir()) == ["i-1.txt", "i-2.txt"]


# --- путь к чистому check: повтор той же загрузки ---------------------------------------------------------


def test_fetching_a_lost_address_again_restores_a_clean_check_without_touching_the_journal(cli, demo):
    journal = (demo / "journal.jsonl").read_bytes()
    drop_source(demo, "И-2")
    assert check_project(demo) != []
    code, out, err = cli("fetch", OTHER, "--json")
    assert code == 0, err
    assert json.loads(out)["source"] == "И-2"  # номер свободен, снимок встал на прежнее место
    assert check_project(demo) == []  # и повтор «выполнено» с тем же ключом принят: источник один, снимок цел
    assert (demo / "journal.jsonl").read_bytes().startswith(journal)  # журнал только дописан
    done = [e for e in journal_of(demo) if e.get("name") == "strateg fetch" and e["status"] == "выполнено"]
    assert len({e["idempotency_hash"] for e in done}) == 2 and len(done) == 3


def test_adding_a_lost_material_again_does_not_overwrite_the_text_left_behind(cli, demo):
    drop_source(demo, "И-3")
    left = (demo / "inputs" / "заметка.txt.txt").read_bytes()
    code, out, err = cli("add", str(demo / "inputs" / "заметка.txt"), "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["text_path"] != "inputs/заметка.txt.txt"  # занятый файл не перезаписан, текст — рядом
    assert (demo / "inputs" / "заметка.txt.txt").read_bytes() == left
    assert (demo / data["text_path"]).is_file()
    assert check_project(demo) == []  # повтор «выполнено» принят: источник один


# --- номер источника не затирает файл без записи ---------------------------------------------------------


@pytest.fixture
def two(cli, net, page_html, home):
    """Проект с двумя источниками из сети (И-1, И-2) и без материалов."""
    net.add(URL, page_html)
    net.add(OTHER, page_html.replace("199", "249"))
    net.add("https://example.com/third", page_html.replace("199", "349"))
    assert cli("new", "demo")[0] == 0
    assert cli("fetch", URL)[0] == 0
    assert cli("fetch", OTHER)[0] == 0
    return home / "demo"


def test_fetch_of_another_address_skips_a_number_whose_snapshot_has_no_record(cli, two):
    left = (two / "sources" / "i-2.txt").read_bytes()
    drop_source(two, "И-2")
    code, out, err = cli("fetch", "https://example.com/third", "--json")
    assert code == 0, err
    assert json.loads(out)["source"] == "И-3"  # номер 2 занят файлом без записи
    assert (two / "sources" / "i-2.txt").read_bytes() == left  # потерянный снимок цел
    assert any("sources/i-2.txt" in e for e in check_project(two))  # и потеря по-прежнему названа


def test_number_skips_a_text_of_a_material_with_personal_data_in_inputs(cli, two):
    (two / "inputs" / "i-3.txt").write_text("чужой файл под служебным именем", encoding="utf-8")
    note = two / "inputs" / "анкета.txt"
    note.write_text("Клиент: телефон +7 (916) 123-45-67, почта ivan.petrov@example.com.", encoding="utf-8")
    code, out, err = cli("add", str(note), "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["source"] == "И-4" and data["text_path"] == "inputs/i-4.txt"  # i-3.txt не тронут
    assert (two / "inputs" / "i-3.txt").read_text(encoding="utf-8") == "чужой файл под служебным именем"


# --- повтор «выполнено» ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["strateg fetch", "strateg add"])
def test_repeated_done_is_harmless_when_there_is_one_source_with_an_intact_snapshot(demo, name):
    repeat_done(demo, name)
    assert check_project(demo) == []
    repeats = [e for e in journal_of(demo) if e.get("name") == name and e.get("status") == "выполнено"]
    assert len(repeats) > len({e["idempotency_hash"] for e in repeats})  # повтор в журнале остался


def test_repeated_done_stays_an_error_when_the_address_has_two_sources(demo):
    def double(d):
        twin = copy.deepcopy(d["sources"][0])
        twin["id"] = "И-4"
        twin["text_path"] = "sources/i-4.txt"
        d["sources"].append(twin)

    shutil.copy(demo / "sources" / "i-1.txt", demo / "sources" / "i-4.txt")
    edit_yaml(demo, "sources.yaml", double)
    rehash(demo)
    assert check_project(demo) == []  # два источника на адрес сами по себе не ошибка
    repeat_done(demo, "strateg fetch", 0)
    errors = check_project(demo)
    assert any("повторная отметка выполнения с тем же ключом" in e for e in errors)


def test_repeated_done_stays_an_error_when_the_record_is_lost(demo):
    repeat_done(demo, "strateg fetch", 1)
    drop_source(demo, "И-2")
    errors = check_project(demo)
    assert any("повторная отметка выполнения с тем же ключом" in e for e in errors)
    assert any("sources/i-2.txt" in e for e in errors)


def test_repeated_done_stays_an_error_when_the_snapshot_is_damaged(demo):
    repeat_done(demo, "strateg fetch", 0)
    (demo / "sources" / "i-1.txt").write_text("совсем другой текст снимка", encoding="utf-8")
    errors = check_project(demo)
    assert any("повторная отметка выполнения с тем же ключом" in e for e in errors)
    assert any("хеш не совпадает" in e for e in errors)


def test_repeated_done_stays_an_error_when_the_snapshot_is_missing(demo):
    repeat_done(demo, "strateg fetch", 0)
    (demo / "sources" / "i-1.txt").unlink()
    errors = check_project(demo)
    assert any("повторная отметка выполнения с тем же ключом" in e for e in errors)


def test_repeated_done_of_another_action_stays_an_error(cli, demo):
    assert cli("step", "start", "1")[0] == 0
    assert cli("step", "done", "1")[0] == 0
    repeat_done(demo, "шаг 1")
    assert any("повторная отметка выполнения" in e for e in check_project(demo))


def test_command_after_a_repeat_does_not_roll_back_because_of_the_old_error(cli, demo):
    repeat_done(demo, "strateg fetch", 0)
    assert cli("step", "start", "1")[0] == 0  # проект чист, команды пишут как обычно
    assert proj.Project.load(demo).outside == []


# --- цепочка перепроверок — один источник ---------------------------------------------------------


NEW_TEXT = (
    "<html><body><article><p>Стоимость подписки в Москве — 249 рублей в месяц. Первые семь дней бесплатно, "
    "дальше списывается абонентская плата. Подписка работает на всех тарифах.</p></article></body></html>"
)


def test_repeat_stays_harmless_after_a_refresh_with_a_changed_text(cli, net, demo):
    repeat_done(demo, "strateg fetch", 0)
    assert check_project(demo) == []
    net.add(URL, NEW_TEXT)
    code, out, err = cli("fetch", URL, "--refresh", "--json")
    assert code == 0, err
    assert json.loads(out)["refresh"] == "new"  # второй источник того же адреса, ссылка refreshes
    assert check_project(demo) == []  # звенья одной цепочки — один источник


def test_repeat_stays_harmless_after_two_refreshes(cli, net, demo):
    repeat_done(demo, "strateg fetch", 0)
    net.add(URL, NEW_TEXT)
    assert cli("fetch", URL, "--refresh")[0] == 0
    net.add(URL, NEW_TEXT.replace("249", "299"))
    assert cli("fetch", URL, "--refresh")[0] == 0
    assert check_project(demo) == []


def test_repeat_is_an_error_for_a_chain_with_a_damaged_last_link(cli, net, demo):
    repeat_done(demo, "strateg fetch", 0)
    net.add(URL, NEW_TEXT)
    assert cli("fetch", URL, "--refresh")[0] == 0
    last = next(p for p in sorted((demo / "sources").iterdir()) if p.name not in ("i-1.txt", "i-2.txt"))
    last.write_text("испорченный снимок", encoding="utf-8")
    assert any("повторная отметка выполнения" in e for e in check_project(demo))


def test_repeat_is_an_error_for_two_unrelated_sources_of_one_address(demo):
    def double(d):
        twin = copy.deepcopy(d["sources"][0])
        twin["id"] = "И-4"
        twin["text_path"] = "sources/i-4.txt"
        d["sources"].append(twin)  # ссылки refreshes нет: это не перепроверка

    shutil.copy(demo / "sources" / "i-1.txt", demo / "sources" / "i-4.txt")
    edit_yaml(demo, "sources.yaml", double)
    rehash(demo)
    repeat_done(demo, "strateg fetch", 0)
    assert any("повторная отметка выполнения" in e for e in check_project(demo))


# --- исходник с ПДн унесён соседом -------------------------------------------------------------------

PII_TEXT = "Клиент: телефон +7 (916) 123-45-67, почта ivan.petrov@example.com."


@pytest.fixture
def carried(cli, home):
    """Проект, где исходник с ПДн уже добавлен и унесён в `.tmp/pending/`: в `inputs/` его нет."""
    assert cli("new", "demo")[0] == 0
    root = home / "demo"
    f = root / "inputs" / "анкета.txt"
    f.write_text(PII_TEXT, encoding="utf-8")
    code, out, err = cli("add", str(f), "--json")
    assert code == 0, err
    assert not f.exists()
    return root, f, json.loads(out)["source"]


def test_register_of_a_file_a_neighbour_has_carried_off_gives_the_same_source(carried):
    from strateg import materials

    root, f, first = carried
    outcome = materials.register(
        proj.Project.load(root), f, None
    )  # файл исчез до чтения: ветка FileNotFoundError
    assert outcome.duplicate and outcome.source.id == first


def test_add_of_a_file_a_neighbour_has_carried_off_gives_the_same_number(cli, carried):
    root, f, first = carried
    code, out, err = cli("add", str(f), "--json")
    data = json.loads(out)
    assert code == 0, err
    assert data["source"] == first and data["duplicate"] is True
    assert len(list((root / ".tmp" / "pending" / first).iterdir())) == 1  # ничего не перенесено повторно


def test_a_vanished_file_nobody_carried_off_is_an_error_with_text(cli, carried):
    root, _, _ = carried
    code, _, err = cli("add", str(root / "inputs" / "нет-такого.txt"))
    assert code == 1 and "не найден" in err and "Traceback" not in err


def test_a_carried_off_file_is_matched_by_its_content_not_by_its_name(carried):
    from strateg import materials

    root, f, first = carried
    (root / ".tmp" / "pending" / first / "анкета.txt").write_text("другое содержимое", encoding="utf-8")
    with pytest.raises(proj.CliError, match="исчез"):  # имя то же, хеш не совпал — это не тот файл
        materials.register(proj.Project.load(root), f, None)
