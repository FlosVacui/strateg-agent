"""`strateg rubric record`: ответы части Б, итог по pass_rule, файл completeness.yaml и журнал."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from helpers import COMPLETE, edit_yaml, journal_of, load_yaml, rehash, tree_hashes
from strateg import rubric
from strateg.project_check import check_project

RUBRIC = rubric.load_rubric()
CRITICAL = [q.id for q in RUBRIC.part_b if q.critical]
SOFT = [q.id for q in RUBRIC.part_b if not q.critical]
IF_QUESTIONS = [q.id for q in RUBRIC.part_b if q.question.startswith("Если")]


@pytest.fixture
def proj(tmp_path, clock) -> Path:
    clock.advance(7200)  # журнал эталона кончается позже стартовых часов теста
    shutil.copytree(COMPLETE, tmp_path / "p")
    return tmp_path / "p"


def answers(**no: str) -> list[dict]:
    """Ответы на все вопросы части Б: «да», кроме номеров в `no` (ключи вида Б_1, дефис заменён)."""
    wrong = {k.replace("_", "-") for k in no}
    return [
        {
            "id": q.id,
            "answer": "нет" if q.id in wrong else "да",
            "where": "options.yaml (recommendation)",
            "why": "расхождение найдено" if q.id in wrong else "проверено по записям",
        }
        for q in RUBRIC.part_b
    ]


def record(cli, proj, data, *extra) -> tuple[int, dict | str, str]:
    code, out, err = cli(
        "rubric", "record", "--project", str(proj), "--file", "-", "--json", *extra, stdin=json.dumps(data)
    )
    return code, (json.loads(out) if out.strip().startswith("{") else out), err


def saved(proj) -> dict:
    return load_yaml(proj / "completeness.yaml")


# --- итог по pass_rule ---------------------------------------------------------


def test_all_yes_passes_and_writes_file_and_event(cli, proj):
    code, data, err = record(cli, proj, answers())
    assert code == 0, err
    assert data["result"] == "пройдено" and data["failed"] == [] and data["changed"] is True
    file = saved(proj)
    assert file["result"] == "пройдено" and file["by"] == "критик" and file["step"] == "7"
    assert len(file["part_a"]) == 25 and len(file["part_b"]) == len(RUBRIC.part_b)
    assert all(a["answer"] == "да" and "message" not in a for a in file["part_a"])
    assert file["part_b"][0] == {
        "id": "Б-1",
        "answer": "да",
        "where": "options.yaml (recommendation)",
        "why": "проверено по записям",
    }
    event = journal_of(proj)[-1]
    assert event["name"] == "strateg rubric record" and event["status"] == "выполнено"
    assert "пройдено" in event["message"] and "критик" in event["message"]
    assert check_project(proj) == []
    from strateg.project import Project

    assert "completeness.yaml" in Project.load(proj).state.files


def test_one_non_critical_no_still_passes(cli, proj):
    code, data, _ = record(cli, proj, answers(**{SOFT[0].replace("-", "_"): "x"}))
    assert code == 0 and data["result"] == "пройдено" and data["failed"] == [SOFT[0]]
    assert saved(proj)["failed"] == [SOFT[0]] and check_project(proj) == []


def test_two_non_critical_no_fail(cli, proj):
    code, data, _ = record(cli, proj, answers(**{s.replace("-", "_"): "x" for s in SOFT[:2]}))
    assert code == 0 and data["result"] == "не пройдено" and data["failed"] == SOFT[:2]
    assert check_project(proj) == []


def test_one_critical_no_fails_and_names_what_to_add(cli, proj):
    code, data, _ = record(cli, proj, answers(**{CRITICAL[0].replace("-", "_"): "x"}))
    assert data["result"] == "не пройдено" and data["failed"] == [CRITICAL[0]]
    assert CRITICAL[0] in data["part_b_failed"] and data["part_b_failed"][CRITICAL[0]]
    code, out, _ = cli(
        "rubric", "record", "--project", str(proj), "--file", "-",
        stdin=json.dumps(answers(**{CRITICAL[1].replace("-", "_"): "x"})),
    )  # fmt: skip
    assert "Что дополнить" in out and "В доработку идёт каждый признак с «нет»" in out


def test_part_a_failure_fails_the_result_and_lists_the_message(cli, proj):
    (proj / "verification.yaml").unlink()
    rehash(proj)
    code, data, _ = record(cli, proj, answers())
    assert code == 0 and data["result"] == "не пройдено"
    assert "А-1" in data["failed"] and "Б-1" not in data["failed"]
    file = saved(proj)
    a1 = next(a for a in file["part_a"] if a["id"] == "А-1")
    assert a1["answer"] == "нет" and "verification.yaml" in a1["message"]
    assert file["failed"] == [a["id"] for a in file["part_a"] if a["answer"] == "нет"]


def test_record_runs_part_a_at_the_current_step(cli, proj):
    edit_yaml(proj, "state.yaml", lambda d: d.update(step="6"))
    edit_yaml(proj, "state.yaml", lambda d: d["gates"].update({"В5": "впереди"}))
    import json as _j

    lines = (proj / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    last = _j.loads(lines[-1])
    last["step"] = "6"
    last["name"] = "шаг 6"
    last["idempotency_hash"] = "b" * 64
    (proj / "journal.jsonl").write_text(
        "\n".join([*lines, _j.dumps(last, ensure_ascii=False)]) + "\n", encoding="utf-8"
    )
    rehash(proj)
    code, data, err = record(cli, proj, answers())
    assert code == 0, err
    assert data["step"] == "6" and len(saved(proj)["part_a"]) == 24


# --- условный вопрос «Если» -------------------------------------------------------


def test_if_question_may_be_yes_when_there_is_no_condition(cli, proj):
    data = answers()
    for item in data:
        if item["id"] in IF_QUESTIONS:
            item["why"] = "условия нет: просмотрены У-1…У-6"
    code, out, err = record(cli, proj, data)
    assert code == 0, err
    assert out["result"] == "пройдено"


def test_no_condition_is_refused_for_a_question_without_if(cli, proj):
    plain = next(q.id for q in RUBRIC.part_b if not q.question.startswith("Если"))
    data = answers()
    next(i for i in data if i["id"] == plain)["why"] = "Условия нет"
    code, out, err = cli("rubric", "record", "--project", str(proj), "--file", "-", stdin=json.dumps(data))
    assert code == 1 and plain in err and "Если" in err
    assert not (proj / "completeness.yaml").exists()


# --- ошибки входа: код 1, номер вопроса, ничего не записано ---------------------------


def refused(cli, proj, data, *needles: str):
    before = tree_hashes(proj)
    code, out, err = cli(
        "rubric", "record", "--project", str(proj), "--file", "-", stdin=json.dumps(data, ensure_ascii=False)
    )
    assert code == 1, (out, err)
    for n in needles:
        assert n in err, (n, err)
    assert tree_hashes(proj) == before
    return err


def test_incomplete_input_names_the_missing_question(cli, proj):
    err = refused(cli, proj, answers()[:-1], RUBRIC.part_b[-1].id, "нет ответа")
    assert f"{RUBRIC.part_b[0].id}:" not in err


def test_dont_know_is_not_an_answer(cli, proj):
    data = answers()
    data[2]["answer"] = "не знаю"
    refused(cli, proj, data, data[2]["id"], "не знаю", "нечего проверить")


def test_answer_other_than_yes_no(cli, proj):
    data = answers()
    data[1]["answer"] = "частично"
    refused(cli, proj, data, data[1]["id"], "«да» или «нет»")
    data[1]["answer"] = True  # YAML читает yes как булево значение
    refused(cli, proj, data, data[1]["id"], "«да» или «нет»")


@pytest.mark.parametrize("key", ["where", "why"])
def test_where_and_why_must_not_be_empty(cli, proj, key):
    data = answers()
    data[3][key] = "  "
    refused(cli, proj, data, data[3]["id"], key)
    del data[3][key]
    refused(cli, proj, data, data[3]["id"], key)


def test_two_answers_to_one_question(cli, proj):
    data = [*answers(), answers()[4]]
    refused(cli, proj, data, data[4]["id"], "больше одного ответа")


def test_unknown_question_and_extra_field_and_bad_shape(cli, proj):
    data = answers()
    data[0]["id"] = "Б-99"
    refused(cli, proj, data, "Б-99")
    data = answers()
    data[0]["id"] = "B-1"  # латинская B
    refused(cli, proj, data, "латинск")
    data = answers()
    data[0]["comment"] = "лишнее"
    refused(cli, proj, data, data[0]["id"], "comment")
    refused(cli, proj, {"Б-1": "да"}, "список")
    refused(cli, proj, [], "список")


def test_input_file_is_read_from_path(cli, proj, tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(answers(), allow_unicode=True), encoding="utf-8")
    code, out, err = cli("rubric", "record", "--project", str(proj), "--file", str(path))
    assert code == 0, err and "Проверка полноты: пройдено" in out
    code, _, err = cli("rubric", "record", "--project", str(proj), "--file", str(tmp_path / "нет.yaml"))
    assert code == 1 and "не найден" in err


# --- повтор, история, независимость ---------------------------------------------------


def test_same_input_again_is_unchanged(cli, proj, clock):
    record(cli, proj, answers())
    first = (proj / "completeness.yaml").read_bytes()
    events = len(journal_of(proj))
    clock.advance(600)
    code, data, err = record(cli, proj, answers())
    assert code == 0 and data["changed"] is False
    assert (proj / "completeness.yaml").read_bytes() == first
    assert len(journal_of(proj)) == events and not (proj / "history" / "completeness.v1.yaml").exists()
    code, out, _ = cli("rubric", "record", "--project", str(proj), "--file", "-", stdin=json.dumps(answers()))
    assert "без изменений" in out


def test_changed_input_keeps_previous_version_in_history(cli, proj, clock):
    record(cli, proj, answers())
    first = (proj / "completeness.yaml").read_bytes()
    clock.advance(600)
    code, data, _ = record(cli, proj, answers(**{SOFT[0].replace("-", "_"): "x"}))
    assert data["changed"] is True and data["history"] == "history/completeness.v1.yaml"
    assert (proj / "history" / "completeness.v1.yaml").read_bytes() == first
    clock.advance(600)
    record(cli, proj, answers(**{CRITICAL[0].replace("-", "_"): "x"}))
    assert (proj / "history" / "completeness.v2.yaml").is_file()
    runs = [e for e in journal_of(proj) if e.get("name") == "strateg rubric record"]
    assert len(runs) == 3 and "не пройдено" in runs[-1]["message"] and CRITICAL[0] in runs[-1]["message"]
    assert check_project(proj) == []


def test_by_agent_is_marked_in_file_and_output(cli, proj):
    code, data, _ = record(cli, proj, answers(), "--by", "агент")
    assert data["by"] == "агент" and data["independent"] is False
    assert saved(proj)["by"] == "агент"
    assert "агент" in journal_of(proj)[-1]["message"]
    code, out, _ = cli(
        "rubric",
        "record",
        "--project",
        str(proj),
        "--file",
        "-",
        "--by",
        "критик",
        stdin=json.dumps(answers()),
    )
    assert code == 0 and "без изменений" not in out  # другой исполнитель — другая запись
    assert saved(proj)["by"] == "критик"
    code, out, _ = cli(
        "rubric",
        "record",
        "--project",
        str(proj),
        "--file",
        "-",
        "--by",
        "агент",
        stdin=json.dumps(answers()),
    )
    assert "не независимая" in out


def test_default_executor_is_critic(cli, proj):
    record(cli, proj, answers())
    assert saved(proj)["by"] == "критик"


def test_unknown_executor_is_a_usage_error(cli, proj):
    code, _, err = cli("rubric", "record", "--project", str(proj), "--file", "-", "--by", "модель")
    assert code == 2


# --- проверка файла в check_project -----------------------------------------------------


def test_check_project_validates_the_file_against_the_rubric(cli, proj):
    record(cli, proj, answers())
    edit_yaml(proj, "completeness.yaml", lambda d: d["part_a"][0].update(id="А-99"))
    errors = check_project(proj)
    assert any("А-99" in e and "нет в рубрике" in e for e in errors)


def test_check_project_flags_unknown_question_and_missing_answer(cli, proj):
    record(cli, proj, answers())
    edit_yaml(proj, "completeness.yaml", lambda d: d["part_b"].pop())
    assert any("нет ответа на вопрос" in e for e in check_project(proj))
    record_again = answers()
    del record_again
    edit_yaml(proj, "completeness.yaml", lambda d: d["part_b"].append({**d["part_b"][0], "id": "Б-77"}))
    assert any("Б-77" in e and "нет в рубрике" in e for e in check_project(proj))


def test_check_project_flags_result_that_disagrees_with_pass_rule(cli, proj):
    record(cli, proj, answers(**{s.replace("-", "_"): "x" for s in SOFT[:2]}))
    assert check_project(proj) == []
    edit_yaml(proj, "completeness.yaml", lambda d: d.update(result="пройдено"))
    assert any("result" in e and "pass_rule" in e for e in check_project(proj))


def test_check_project_flags_inconsistent_failed_list(cli, proj):
    record(cli, proj, answers(**{SOFT[0].replace("-", "_"): "x"}))
    edit_yaml(proj, "completeness.yaml", lambda d: d.update(failed=[]))
    assert any("failed" in e for e in check_project(proj))


def test_hand_edit_of_the_file_is_noticed_as_outside_change(cli, proj):
    record(cli, proj, answers())
    edit_yaml(proj, "completeness.yaml", lambda d: d.update(by="агент"))
    from strateg.project import Project

    assert ("completeness.yaml", "хеш файла не совпал с записанным") in Project.load(proj).outside
