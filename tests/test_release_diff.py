"""Снимок выпуска, шаг 8 (новое решение вместо прежнего, правка утверждений) и `strateg diff` (ADR-0016)."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from helpers import journal_of, load_yaml, tree_hashes
from strateg import project as proj
from strateg.project_check import check_project

FIXTURE = Path(__file__).parent / "fixtures" / "project_follow_up"
OUTPUT_FILES = ("frame", "plan", "gaps", "options", "promises", "verification", "decisions")
DOCS = ("comms-strategy", "one-pager", "product-strategy", "situation")


@pytest.fixture
def fu(home, clock) -> Path:
    """Проект с начатым шагом 7; часы — 2027-02-01."""
    shutil.copytree(FIXTURE, home / "fu")
    clock.value = dt.datetime(2027, 2, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    return home / "fu"


def run(cli, *args):
    code, out, err = cli(*args, "--project", "fu", "--json")
    assert code == 0, (out, err)
    return json.loads(out)


def fail(cli, *args):
    code, out, err = cli(*args, "--project", "fu", "--json")
    assert code == 1, (out, err)
    return json.loads(out)["error"]


def released(cli, fu) -> Path:
    run(cli, "step", "done", "7")
    return fu / "history" / "release-1"


def write(cli, **kinds):
    code, out, err = cli(
        "write", "--file", "-", "--project", "fu", stdin=json.dumps(kinds, ensure_ascii=False)
    )
    assert code == 0, (out, err)
    return out


def raw(fu: Path, kind: str) -> dict:
    data = load_yaml(fu / f"{kind}.yaml")
    data.pop("schema_version")
    return data


# --- снимок выпуска -------------------------------------------------------------------------------


def test_step_done_7_saves_the_release_snapshot(cli, fu):
    data = run(cli, "step", "done", "7")
    assert data["release"] == 1
    base = fu / "history" / "release-1"
    assert sorted(p.name for p in base.glob("*.yaml")) == sorted(f"{k}.yaml" for k in OUTPUT_FILES)
    assert sorted(p.stem for p in (base / "outputs").glob("*.md")) == sorted(DOCS)
    for kind in OUTPUT_FILES:  # байт в байт
        assert (base / f"{kind}.yaml").read_bytes() == (fu / f"{kind}.yaml").read_bytes()
    assert (base / "outputs" / "one-pager.md").read_bytes() == (fu / "outputs" / "one-pager.md").read_bytes()
    assert check_project(fu) == []
    done = journal_of(fu)[-1]
    assert done["name"] == "шаг 7" and done["status"] == "выполнено" and done["actor"] == "CLI"
    assert done["message"] == "снимок выпуска 1: файлов 11, history/release-1/"
    files = load_yaml(fu / "state.yaml")["files"]
    assert "history/release-1/options.yaml" in files and "history/release-1/outputs/one-pager.md" in files


def test_repeating_step_done_7_does_not_take_a_second_snapshot(cli, fu):
    released(cli, fu)
    again = run(cli, "step", "done", "7")
    assert again["already_done"] is True and "release" not in again
    assert sorted(p.name for p in (fu / "history").glob("release-*")) == ["release-1"]


def test_only_markdown_documents_enter_the_snapshot(cli, fu):
    (fu / "outputs" / "notes.txt").write_text("черновик", encoding="utf-8")
    (fu / "outputs" / "draft.md").write_text("# Черновик\n", encoding="utf-8")
    base = released(cli, fu)
    assert not (base / "outputs" / "notes.txt").exists() and (base / "outputs" / "draft.md").is_file()
    assert check_project(fu) == []


def test_release_without_any_output_files_has_no_snapshot(cli, home):
    assert cli("new", "demo")[0] == 0
    assert cli("step", "start", "7")[0] == 0
    code, out, _ = cli("step", "done", "7", "--json")
    assert code == 0 and "release" not in json.loads(out)
    assert not (home / "demo" / "history").exists()
    code, out, _ = cli("diff", "--json")
    assert code == 1 and "снимка выпуска нет" in json.loads(out)["error"]


def test_snapshot_goes_into_the_archive_like_the_rest_of_history(cli, fu, tmp_path):
    released(cli, fu)
    out = tmp_path / "fu.zip"
    run(cli, "pack", "--out", str(out))
    names = zipfile.ZipFile(out).namelist()
    assert "fu/history/frame.v1.yaml" in names
    assert "fu/history/release-1/options.yaml" in names
    assert "fu/history/release-1/outputs/one-pager.md" in names
    assert not any(".tmp" in n for n in names)


def test_check_accepts_only_release_files_inside_a_snapshot(cli, fu):
    base = released(cli, fu)
    (base / "random.txt").write_text("лишнее", encoding="utf-8")
    errors = check_project(fu)
    assert any("history/release-1/random.txt" in e and "снимке выпуска" in e for e in errors), errors
    (base / "random.txt").unlink()
    (fu / "history" / "notes.txt").write_text("x", encoding="utf-8")
    errors = check_project(fu)
    assert any("history/notes.txt" in e and "release-<n>" in e for e in errors), errors


def test_edit_of_a_snapshot_outside_the_agent_is_noticed(cli, fu):
    base = released(cli, fu)
    with (base / "options.yaml").open("a", encoding="utf-8", newline="\n") as f:
        f.write("# правка\n")
    run(cli, "step", "start", "8")
    kinds = [e for e in journal_of(fu) if e["kind"] == "изменено вне агента"]
    assert [e["path"] for e in kinds] == ["history/release-1/options.yaml"]


# --- шаг 8 ----------------------------------------------------------------------------------------


def replacing_decision(fu: Path) -> dict:
    """Решение Реш-2 вместо Реш-1: другой вариант, свои условия и срок."""
    data = raw(fu, "decisions")
    old = data["decisions"][0]
    new = {
        **old,
        "id": "Реш-2",
        "option": "Вар-3",
        "rejected": ["Вар-1", "Вар-2", "Вар-4"],
        "expected": "Выручка тарифов вырастет",
        "conditions": [{**old["conditions"][0], "date": "2027-06-01", "action": "вернуться к Вар-2"}],
        "review_date": "2027-06-01",
        "commitments": [],
        "replaces": "Реш-1",
    }
    old["status"] = "заменено"
    old["replaced_by"] = "Реш-2"
    data["decisions"].append(new)
    return data


def test_step_8_starts_after_release_and_writes_a_replacing_decision(cli, fu):
    released(cli, fu)
    started = run(cli, "step", "start", "8")
    assert started["step"] == "8" and started["attempt"] == 1
    out = write(cli, decisions=replacing_decision(fu))
    assert "decisions.yaml: перезаписан" in out
    assert check_project(fu) == []
    decisions = {d["id"]: d for d in raw(fu, "decisions")["decisions"]}
    assert decisions["Реш-1"]["status"] == "заменено" and decisions["Реш-1"]["replaced_by"] == "Реш-2"
    assert decisions["Реш-2"]["status"] == "действует" and decisions["Реш-2"]["replaces"] == "Реш-1"
    assert (fu / "history" / "decisions.v1.yaml").is_file()  # прежнее решение осталось
    event = [e for e in journal_of(fu) if e.get("name") == "strateg write decisions"][-1]
    assert event["step"] == "8" and event["actor"] == "CLI"
    # дальше сопровождение следит за новым решением
    code, out, _ = cli("due", "--today", "2027-06-01", "--project", "fu", "--json")
    review = json.loads(out)["groups"]["review"]
    assert [i["decision"] for i in review] == ["Реш-2"]


def test_step_8_accepts_claim_changes_and_they_stay_checked(cli, fu, tmp_path):
    released(cli, fu)
    run(cli, "step", "start", "8")
    run(cli, "claim", "revoke", "У-3", "--reason", "интервью не подтвердило")
    new = {
        "text": "За год доля абонентов с подключённой защитой вырастет до 12–14 %",
        "type": "прогноз",
        "subject": "доля абонентов с защитой",
        "period": "2027",
        "basis": ["У-2"],
        "forecast_range": {"low": 12, "high": 14, "unit": "% абонентов"},
    }
    path = tmp_path / "new.yaml"
    path.write_text(yaml.safe_dump(new, allow_unicode=True), encoding="utf-8")
    replaced = run(cli, "claim", "replace", "У-4", "--file", str(path), "--by", "пользователь")
    assert replaced["id"] == "У-10"
    claims = {c["id"]: c for c in load_yaml(fu / "claims.yaml")["claims"]}
    assert claims["У-3"]["status"] == "отозвано" and claims["У-4"]["status"] == "заменено"
    assert claims["У-10"]["step"] == "8" and claims["У-10"]["replaces"] == "У-4"
    # рекомендация ещё стоит на У-4: проверка называет это, а `write` исправляет
    assert any("рекомендацию" in e for e in check_project(fu))
    opts = raw(fu, "options")
    opts["recommendation"]["claims"] = ["У-2", "У-10"]
    for o in opts["options"]:
        for cell in ("effect", "first_signal"):
            if o["comparison"][cell].get("forecast") == "У-4":
                o["comparison"][cell]["forecast"] = "У-10"
    decisions = raw(fu, "decisions")
    decisions["decisions"][0]["basis"] = ["У-2", "У-10", "У-7"]
    write(cli, options=opts, decisions=decisions)
    assert check_project(fu) == []
    assert {e["actor"] for e in journal_of(fu)[-8:]} <= {"CLI", "пользователь"}  # событий хука нет


def test_pause_after_a_stop_survives_step_8_commands(cli, fu):
    released(cli, fu)
    run(cli, "step", "start", "8")
    run(cli, "stop", "--reason", "выбор на В4 задет, нужен ответ человека")
    assert proj.pause_kind(proj.Project.load(fu).journal) == proj.PAUSE_STOP
    for argv in (("due", "--today", "2027-02-01"), ("diff",), ("impact", "У-2")):
        run(cli, *argv)
    assert proj.pause_kind(proj.Project.load(fu).journal) == proj.PAUSE_STOP


# --- diff -----------------------------------------------------------------------------------------


def test_diff_without_a_snapshot_explains_where_it_comes_from(cli, fu):
    assert "step done 7" in fail(cli, "diff")


def test_diff_right_after_release_reports_no_changes(cli, fu):
    released(cli, fu)
    data = run(cli, "diff")
    assert data == {"ok": True, "release": 1, "changed": False, "files": [], "documents": []}
    code, out, _ = cli("diff", "--project", "fu")
    assert code == 0 and out.strip() == "Изменений после выпуска нет."


def revise(cli, fu: Path) -> None:
    """Пересмотр после выпуска: новое решение, ответ рекомендации, допущение варианта, правка документа."""
    run(cli, "step", "start", "8")
    opts = raw(fu, "options")
    vs3 = next(o for o in opts["options"] if o["id"] == "Вар-3")
    vs3["main_assumption"] = "Абоненты примут рост цены тарифа"
    opts["recommendation"]["answer"] = "Пробный период и первые продления покажут готовность платить за месяц"
    write(cli, options=opts, decisions=replacing_decision(fu))
    page = fu / "outputs" / "one-pager.md"
    text = page.read_text(encoding="utf-8").replace("## Кратко", "## Итог")
    page.write_text(text + "\n## Что изменилось\nВыбор пересмотрен.\n", encoding="utf-8", newline="\n")


def test_diff_names_changed_records_by_number_and_sections_by_heading(cli, fu):
    released(cli, fu)
    revise(cli, fu)
    data = run(cli, "diff")
    assert data["release"] == 1 and data["changed"] is True
    files = {f["file"]: f for f in data["files"]}
    assert set(files) == {"options.yaml", "decisions.yaml"}
    opts = files["options.yaml"]
    assert opts["added"] == [] and opts["removed"] == []
    assert [c["id"] for c in opts["changed"]] == ["Вар-3"]
    assert opts["changed"][0]["fields"] == [
        {
            "field": "main_assumption",
            "was": "Абоненты не откажутся от более дорогого тарифа",
            "now": "Абоненты примут рост цены тарифа",
        }
    ]
    assert [b["block"] for b in opts["blocks"]] == ["рекомендация"]
    dec = files["decisions.yaml"]
    assert dec["added"] == ["Реш-2"] and dec["removed"] == []
    assert [c["id"] for c in dec["changed"]] == ["Реш-1"]
    assert {f["field"]: (f["was"], f["now"]) for f in dec["changed"][0]["fields"]} == {
        "status": ("действует", "заменено"),
        "replaced_by": (None, "Реш-2"),
    }
    (doc,) = data["documents"]
    assert doc["path"] == "outputs/one-pager.md" and doc["status"] == "изменён"
    assert (
        doc["added"] == ["Итог", "Что изменилось"] and doc["removed"] == ["Кратко"] and doc["changed"] == []
    )


def test_diff_text_is_was_to_became(cli, fu):
    released(cli, fu)
    revise(cli, fu)
    code, out, _ = cli("diff", "--project", "fu")
    assert code == 0
    assert out.splitlines()[0] == "Изменения после выпуска 1 (было → стало):"
    assert "Решения:" in out and "добавлено: Реш-2" in out
    assert "Реш-1, status: было действует → стало заменено" in out
    assert (
        "Вар-3, main_assumption: было Абоненты не откажутся от более дорогого тарифа"
        " → стало Абоненты примут рост цены тарифа"
    ) in out
    assert "outputs/one-pager.md: добавлены разделы «Итог», «Что изменилось»; убраны разделы «Кратко»" in out


def test_diff_ignores_whitespace_and_reports_changed_section_text(cli, fu):
    released(cli, fu)
    page = fu / "outputs" / "situation.md"
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("\n\n", "\n\n\n") + "\n\n", encoding="utf-8", newline="\n")
    assert run(cli, "diff")["changed"] is False  # только пустые строки
    page.write_text(text.replace("Нет.", "Источники расходятся по цене."), encoding="utf-8", newline="\n")
    (doc,) = run(cli, "diff")["documents"]
    assert doc["changed"] == ["Где источники спорят"] and doc["added"] == [] and doc["removed"] == []


def test_diff_sees_added_and_removed_documents(cli, fu):
    released(cli, fu)
    (fu / "outputs" / "run-report.md").write_text("# Отчёт о прогоне\n\n## Итог\nГотово.\n", encoding="utf-8")
    (fu / "outputs" / "situation.md").unlink()
    docs = {d["path"]: d["status"] for d in run(cli, "diff")["documents"]}
    assert docs == {"outputs/run-report.md": "добавлен", "outputs/situation.md": "удалён"}


def test_diff_picks_the_latest_release_and_takes_a_number(cli, fu):
    released(cli, fu)
    revise(cli, fu)
    run(cli, "step", "start", "7")
    second = run(cli, "step", "done", "7")
    assert second["release"] == 2
    assert run(cli, "diff")["release"] == 2 and run(cli, "diff")["changed"] is False
    first = run(cli, "diff", "--release", "1")
    assert first["release"] == 1 and first["changed"] is True
    assert "снимка выпуска 3 нет; есть: 1, 2" in fail(cli, "diff", "--release", "3")
    assert check_project(fu) == []


def test_diff_is_read_only(cli, fu):
    released(cli, fu)
    revise(cli, fu)
    before = tree_hashes(fu)
    assert cli("diff", "--project", "fu")[0] == 0
    assert tree_hashes(fu) == before


def test_project_released_before_snapshots_gets_a_hint(cli, fu):
    # шаг 7 выполнен давно, папки release-1 нет: сравнивать не с чем
    assert "снимка выпуска нет" in fail(cli, "diff")


# --- решение из снимка выпуска нельзя убрать ------------------------------------------------------


def only_new_decision(fu: Path) -> dict:
    """Файл решений без Реш-1: одно новое решение, ссылки на прежнее нет."""
    data = replacing_decision(fu)
    new = data["decisions"][1]
    new.pop("replaces")
    return {"decisions": [new]}


def test_a_released_decision_cannot_be_dropped_from_the_file(cli, fu):
    released(cli, fu)
    run(cli, "step", "start", "8")
    before = (fu / "decisions.yaml").read_bytes()
    code, out, err = cli(
        "write", "--file", "-", "--project", "fu", "--json",
        stdin=json.dumps({"decisions": only_new_decision(fu)}, ensure_ascii=False),
    )  # fmt: skip
    assert code == 1, (out, err)
    message = json.loads(out)["error"]
    assert "Реш-1 было в снимке выпуска 1 и пропало из decisions.yaml" in message
    assert "replaces" in message and "отменено" in message
    assert (fu / "decisions.yaml").read_bytes() == before  # ничего не записано
    assert not (fu / "history" / "decisions.v1.yaml").exists()


def test_a_released_decision_can_be_replaced_or_cancelled(cli, fu):
    released(cli, fu)
    run(cli, "step", "start", "8")
    write(cli, decisions=replacing_decision(fu))  # заменено: Реш-1 остаётся в файле со статусом
    data = raw(fu, "decisions")
    data["decisions"][0].pop("replaced_by")
    data["decisions"][0]["status"] = "отменено"
    data["decisions"][1].pop("replaces")
    write(cli, decisions=data)  # отменено: тоже остаётся в файле
    assert check_project(fu) == []


def test_before_release_a_decision_can_still_be_dropped(cli, fu):
    # снимка выпуска нет: проверять нечего, правка черновика решений свободна
    write(cli, decisions=only_new_decision(fu))
    assert [d["id"] for d in raw(fu, "decisions")["decisions"]] == ["Реш-2"]
