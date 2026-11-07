"""Команда `strateg write`: файлы выхода по схемам, версии в `history/`, откат, рамка и ворота В1."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil

import pytest
import yaml

from helpers import (
    FIXTURE,
    FRAME,
    KINDS,
    fact,
    fixture_outputs,
    journal_of,
    load_yaml,
    put,
    put_frame,
    tree_hashes,
    write_claims,
)
from strateg import outputs
from strateg.models import FrameFile
from strateg.project_check import check_project


@pytest.fixture
def example(home, clock):
    """Копия примера проекта; часы позже последнего события примера, иначе время в журнале убывало бы."""
    clock.advance(24 * 3600)
    dst = home / "example"
    shutil.copytree(FIXTURE, dst)
    assert check_project(dst) == []
    return dst


def write_in(cli, project: str, data: dict, *extra: str):
    return cli(
        "write", "--file", "-", "--project", project, *extra, stdin=json.dumps(data, ensure_ascii=False)
    )


def parsed(kind: str, data: dict) -> dict:
    """Содержимое после разбора моделью, без служебных полей: умолчания (`closed_by: []`) дописаны."""
    model, errors = outputs.validate(outputs.KINDS[kind], data)
    assert model is not None, errors
    out = model.model_dump(mode="json", exclude_none=True)
    out.pop("schema_version")
    out.pop("version", None)
    return out


def put_yaml(path, data: dict) -> None:
    """Файл проекта руками: UTF-8 и LF, как у CLI (на Windows `write_text` дал бы CRLF)."""
    path.write_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"))


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- каждый вид: запись, версия, хеш, событие ---

MUTATIONS = {
    "frame": lambda d: d["deadline"].update(value="к пятнице"),
    "plan": lambda d: d["estimate"].update(minutes=41),
    "gaps": lambda d: d["gaps"][0].update(text="Доля отключений в первый месяц неизвестна"),
    "options": lambda d: d["options"][0].update(main_assumption="Абоненты находят подписку без подсказок"),
    "promises": lambda d: d["promises"][0].update(conditions="Москва, 2026 год"),
    "verification": lambda d: d["rows"][0].update(deadline="через 9 недель после запуска"),
    "decisions": lambda d: d["decisions"][0].update(expected="Доля абонентов с защитой вырастет за год"),
}


@pytest.mark.parametrize("kind", KINDS)
def test_write_replaces_a_file_and_keeps_the_previous_one(cli, example, kind):
    before = (example / f"{kind}.yaml").read_bytes()
    data = fixture_outputs()[kind]
    MUTATIONS[kind](data)
    code, out, err = write_in(cli, "example", {kind: data})
    assert code == 0, err
    assert f"{kind}.yaml" in out

    new = load_yaml(example / f"{kind}.yaml")
    assert new["schema_version"] == 1
    new.pop("schema_version")
    new.pop("version", None)
    assert parsed(kind, new) == parsed(kind, data)
    # прежний файл лежит в history/ байт в байт; frame в примере — версия 2, history/frame.v1.yaml уже есть
    expected_n = 2 if kind == "frame" else 1
    assert (example / "history" / f"{kind}.v{expected_n}.yaml").read_bytes() == before
    # хеши в state.files совпадают с файлами, иначе status назвал бы их «изменёнными вне агента»
    files = load_yaml(example / "state.yaml")["files"]
    assert files[f"{kind}.yaml"] == sha(example / f"{kind}.yaml")
    assert files[f"history/{kind}.v{expected_n}.yaml"] == sha(
        example / "history" / f"{kind}.v{expected_n}.yaml"
    )
    assert files["journal.jsonl"] == sha(example / "journal.jsonl")
    # событие журнала на каждый изменённый файл
    last = journal_of(example)[-1]
    assert last["kind"] == "действие" and last["name"] == f"strateg write {kind}"
    assert last["status"] == "выполнено" and last["actor"] == "CLI"
    assert check_project(example) == []


def test_write_all_kinds_from_one_input_is_one_event_per_file(cli, example):
    data = fixture_outputs()
    for kind in KINDS:
        MUTATIONS[kind](data[kind])
    before = len(journal_of(example))
    code, out, err = write_in(cli, "example", data)
    assert code == 0, err
    events = journal_of(example)[before:]
    assert [e["name"] for e in events] == [f"strateg write {k}" for k in KINDS]
    assert check_project(example) == []


# --- повтор без изменений ---


def test_same_content_is_no_change_and_writes_nothing(cli, example):
    data = fixture_outputs()
    before = tree_hashes(example)
    code, out, err = write_in(cli, "example", data)
    assert code == 0, err
    assert "без изменений" in out
    assert tree_hashes(example) == before  # ни записи, ни события, ни версии в history/


def test_same_content_in_other_notation_is_no_change(cli, example):
    """Сравнение идёт после разбора моделью: другой порядок ключей и вид записи ничего не меняют."""
    data = fixture_outputs()
    plan = data["plan"]
    plan["questions"][0]["branches"].reverse()
    plan["questions"][0]["branches"].reverse()
    reordered = {"estimate": plan["estimate"], "excluded": plan["excluded"], "questions": plan["questions"]}
    before = tree_hashes(example)
    code, out, _ = write_in(cli, "example", {"plan": reordered})
    assert code == 0 and "без изменений" in out
    assert tree_hashes(example) == before


def test_json_output_says_what_changed(cli, example):
    data = fixture_outputs()
    data["plan"]["estimate"]["minutes"] = 41
    code, out, _ = write_in(cli, "example", {"plan": data["plan"], "decisions": data["decisions"]}, "--json")
    assert code == 0
    files = {f["kind"]: f for f in json.loads(out)["files"]}
    assert files["plan"]["changed"] is True and files["plan"]["history"] == "history/plan.v1.yaml"
    assert files["decisions"]["changed"] is False


# --- номера версий в history/ ---


def test_history_takes_next_free_number(cli, example):
    versions = []
    for minutes in (41, 42, 43):
        data = fixture_outputs()["plan"]
        data["estimate"]["minutes"] = minutes
        versions.append((example / "plan.yaml").read_bytes())
        assert write_in(cli, "example", {"plan": data})[0] == 0
    for n, old in enumerate(versions, start=1):
        assert (example / "history" / f"plan.v{n}.yaml").read_bytes() == old
    assert check_project(example) == []  # имена версий проходят проверку history/


def test_history_number_skips_taken_ones(cli, example):
    (example / "history" / "plan.v4.yaml").write_bytes((example / "plan.yaml").read_bytes())
    data = fixture_outputs()["plan"]
    data["estimate"]["minutes"] = 41
    assert write_in(cli, "example", {"plan": data})[0] == 0
    assert (example / "history" / "plan.v5.yaml").is_file()


# --- проверка входа ---


def test_input_is_checked_by_the_model_before_writing(cli, example):
    data = fixture_outputs()["plan"]
    data["questions"][0]["impact"] = "огромное"
    before = tree_hashes(example)
    code, _, err = write_in(cli, "example", {"plan": data})
    assert code == 1
    assert "plan.yaml, запись ВП-1 (№1), поле impact: недопустимое значение «огромное»" in err
    assert tree_hashes(example) == before


def test_errors_name_the_kind_of_every_broken_file(cli, example):
    data = fixture_outputs()
    data["plan"]["questions"][0]["impact"] = "огромное"
    data["promises"]["promises"][0]["status"] = "возможно"
    code, _, err = write_in(cli, "example", {"plan": data["plan"], "promises": data["promises"]})
    assert code == 1 and "plan.yaml" in err and "promises.yaml" in err


def test_yaml_keys_if_and_from_are_the_file_keys(cli, example):
    """В файле ключи `if` и `from`; поле `if_` в файле — ошибка, а `if` пишется и читается."""
    data = fixture_outputs()["plan"]
    data["questions"][0]["branches"][0]["if_"] = data["questions"][0]["branches"][0].pop("if")
    code, _, err = write_in(cli, "example", {"plan": data})
    assert code == 1 and "if_" in err
    ok = fixture_outputs()["plan"]
    ok["estimate"]["minutes"] = 41
    assert write_in(cli, "example", {"plan": ok})[0] == 0
    assert "- if: " in (example / "plan.yaml").read_text(encoding="utf-8")
    assert "from: " in (example / "options.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ({"frames": {}}, "вид «frames» не бывает"),
        ({"plan": []}, "ожидается набор полей"),
        ([], "нужен набор"),
        ("plan", "нужен набор"),
    ],
)
def test_bad_shape_of_input(cli, example, data, needle):
    code, _, err = write_in(cli, "example", data)
    assert code == 1 and needle in err
    if data != {"plan": []}:
        assert "frame, plan, gaps" in err  # допустимые виды названы


def test_version_in_input_is_a_usage_error(cli, example):
    data = fixture_outputs()["frame"]
    data["version"] = 7
    code, _, err = write_in(cli, "example", {"frame": data})
    assert code == 2 and "version" in err and "ставит CLI" in err
    plan = fixture_outputs()["plan"]
    plan["schema_version"] = 1
    code, _, err = write_in(cli, "example", {"plan": plan})
    assert code == 2 and "schema_version" in err


def test_broken_file_and_missing_file(cli, example, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("plan: [\n", encoding="utf-8")
    code, _, err = cli("write", "--file", str(bad), "--project", "example")
    assert code == 1 and "не читается как YAML" in err
    assert cli("write", "--file", str(tmp_path / "nope.yaml"), "--project", "example")[0] == 1


def test_input_from_a_file(cli, example, tmp_path):
    data = fixture_outputs()["plan"]
    data["estimate"]["minutes"] = 41
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump({"plan": data}, allow_unicode=True), encoding="utf-8")
    assert cli("write", "--file", str(path), "--project", "example")[0] == 0
    assert load_yaml(example / "plan.yaml")["estimate"]["minutes"] == 41


# --- откат ---


def test_new_error_rolls_everything_back_byte_for_byte(cli, example):
    """Файл записан, прежний лёг в history/, и только `check` увидел новую ошибку: всё как до команды."""
    options = fixture_outputs()["options"]
    options["recommendation"]["claims"] = ["У-2", "У-99"]  # такого утверждения нет
    plan = fixture_outputs()["plan"]
    plan["estimate"]["minutes"] = 41
    before = tree_hashes(example)
    code, out, err = write_in(cli, "example", {"plan": plan, "options": options})
    assert code == 1
    assert "У-99" in err and "изменения не записаны" in err
    assert tree_hashes(example) == before  # журнал, state.yaml и history/ тоже
    assert not (example / "history" / "plan.v1.yaml").exists()


def test_refusal_prints_only_new_errors_and_old_ones_are_a_warning(cli, example):
    plan = fixture_outputs()["plan"]
    plan["estimate"]["minutes"] = 41
    (example / "promises.yaml").write_text(
        "schema_version: 1\npromises: нет\n", encoding="utf-8"
    )  # прежняя ошибка
    old = check_project(example)
    assert old
    code, out, err = write_in(cli, "example", {"plan": plan})
    assert code == 0, err
    assert "прежние ошибки проверки: " + str(len(old)) in out
    options = fixture_outputs()["options"]
    options["recommendation"]["claims"] = ["У-99"]
    code, out, err = write_in(cli, "example", {"options": options})
    assert code == 1
    assert "У-99" in err
    assert "promises.yaml" not in err  # прежняя ошибка в отказ не попала


def test_new_project_is_unchanged_after_refusal_even_without_history(cli, home):
    """Папка `history/` создаётся записью прежней версии; после отката её нет."""
    assert cli("new", "demo")[0] == 0
    put_frame(cli)
    before = tree_hashes(home / "demo")
    plan = {
        "questions": [
            {
                "id": "ВП-1",
                "text": "Сколько стоит?",
                "impact": "высокое",
                "branches": [{"if": "дорого", "then": "в тариф"}, {"if": "дёшево", "then": "отдельно"}],
                "close_cost": {"minutes": 3},
                "where": "сайт",
                "status": "пробел",
                "gap": "П-1",  # gaps.yaml нет: ссылка новая ошибка
            }
        ],
        "estimate": {"minutes": 30},
    }
    code, _, err = write_in(cli, "demo", {"plan": plan})
    assert code == 1
    assert tree_hashes(home / "demo") == before
    assert not (home / "demo" / "history").exists()


# --- взаимные ссылки ---


def test_reference_to_a_file_not_written_yet_hints_one_input(cli, home, net, page_html, tmp_path):
    from helpers import URL

    net.add(URL, page_html)
    assert cli("new", "demo")[0] == 0
    assert cli("fetch", URL, "--region", "RU-MOW")[0] == 0
    claims = [
        fact(plan_question="ВП-1"),
        {
            "text": "Подключают чаще семьи с пожилыми родителями",
            "type": "гипотеза",
            "subject": "кто подключает защиту",
            "basis": ["У-1"],
            "check_method": "интервью",
        },
        {
            "text": "Доля абонентов вырастет до 15–17 %",
            "type": "прогноз",
            "subject": "доля абонентов",
            "basis": ["У-1"],
            "forecast_range": {"low": 15, "high": 17, "unit": "%"},
        },
        {
            "text": "Часть абонентов отключит защиту",
            "type": "гипотеза",
            "subject": "отключение",
            "basis": ["У-1"],
            "check_method": "выгрузка",
        },
    ]
    assert cli("claim", "add", "--file", write_claims(tmp_path, claims))[0] == 0
    remap = {2: 1, 3: 2, 4: 3, 5: 4, 6: 4}
    data = fixture_outputs(remap)
    gaps, options = data["gaps"], data["options"]
    before = tree_hashes(home / "demo")
    code, _, err = write_in(cli, "demo", {"gaps": gaps})
    assert code == 1
    assert "options.yaml" in err and "одним входом" in err
    assert tree_hashes(home / "demo") == before
    code, out, err = write_in(cli, "demo", {"gaps": gaps, "options": options})
    assert code == 0, err
    assert check_project(home / "demo") == []
    assert (home / "demo" / "gaps.yaml").is_file() and (home / "demo" / "options.yaml").is_file()


# --- кто правит ---


def test_user_edit_is_a_correction_event(cli, example):
    data = fixture_outputs()["plan"]
    data["estimate"]["minutes"] = 41
    code, _, err = write_in(cli, "example", {"plan": data}, "--by", "пользователь")
    assert code == 0, err
    events = journal_of(example)[-2:]
    assert [e["kind"] for e in events] == ["действие", "исправление"]
    assert events[1]["actor"] == "пользователь" and "plan.yaml" in events[1]["message"]
    data["estimate"]["minutes"] = 42
    assert write_in(cli, "example", {"plan": data})[0] == 0
    assert journal_of(example)[-1]["kind"] == "действие"  # по умолчанию правит агент


# --- рамка до и после ответа на В1 ---


def test_frame_is_a_draft_until_b1_is_answered(cli, home):
    assert cli("new", "demo")[0] == 0
    put_frame(cli)
    root = home / "demo"
    assert load_yaml(root / "frame.yaml")["version"] == 1
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    before = len(journal_of(root))
    put_frame(cli, deadline={"value": "к пятнице", "origin": "ответ человека"})
    assert load_yaml(root / "frame.yaml")["deadline"]["value"] == "к пятнице"
    assert load_yaml(root / "frame.yaml")["version"] == 1  # черновик: версия не растёт
    assert not (root / "history").exists()  # и прежний черновик в history/ не кладётся
    assert len(journal_of(root)) == before + 1  # но событие записи есть
    assert check_project(root) == []


def test_frame_gets_versions_after_b1_answer(cli, home):
    assert cli("new", "demo")[0] == 0
    root = home / "demo"
    put_frame(cli)
    cli("gate", "open", "В1", "--questions", "1")
    put_frame(cli, deadline={"value": "к пятнице", "origin": "ответ человека"})
    assert cli("gate", "answer", "В1", "--answer", "ок")[0] == 0
    answered = (root / "frame.yaml").read_bytes()
    answer_event = journal_of(root)[-1]["gate"]
    assert answer_event["frame"]["deadline"]["value"] == "к пятнице"  # рамка скопирована в событие

    put_frame(cli, deadline={"value": "через неделю", "origin": "ответ человека"})
    assert load_yaml(root / "frame.yaml")["version"] == 2
    assert (root / "history" / "frame.v1.yaml").read_bytes() == answered
    put_frame(cli, deadline={"value": "через месяц", "origin": "ответ человека"})
    assert load_yaml(root / "frame.yaml")["version"] == 3
    assert load_yaml(root / "history" / "frame.v2.yaml")["deadline"]["value"] == "через неделю"
    assert check_project(root) == []  # рамка ответа совпадает с версией 1 из history/
    out = put_frame(cli, deadline={"value": "через месяц", "origin": "ответ человека"})
    assert "без изменений" in out
    assert load_yaml(root / "frame.yaml")["version"] == 3


def test_gate_b1_answer_needs_a_frame(cli, home):
    assert cli("new", "demo")[0] == 0
    cli("gate", "open", "В1", "--questions", "1")
    code, _, err = cli("gate", "answer", "В1", "--answer", "ок")
    assert code == 1
    assert "рамки нет" in err and "strateg write" in err
    state = load_yaml(home / "demo" / "state.yaml")
    assert state["gates"]["В1"] == "ждут ответа"  # ничего не записано
    assert "answer" not in json.dumps(journal_of(home / "demo"), ensure_ascii=False)


def test_other_gates_do_not_need_a_frame(cli, home):
    assert cli("new", "demo")[0] == 0
    cli("gate", "open", "В2", "--questions", "1")
    assert cli("gate", "answer", "В2", "--answer", "ок")[0] == 0
    assert "frame" not in journal_of(home / "demo")[-1]["gate"]


def test_frame_of_b1_answer_must_match_a_version(cli, example):
    """Сверка рамки В1: файл правили, не заводя версию, и рамка в ответе не совпала ни с чем."""
    frame = load_yaml(example / "frame.yaml")
    frame["deadline"]["value"] = "правка руками"
    put_yaml(example / "frame.yaml", frame)
    errors = check_project(example)
    assert any("рамка в ответе на В1 не совпадает" in e for e in errors)


def test_frame_of_b1_answer_may_match_a_version_in_history(example):
    """Рамка В1 совпала с прежней версией в history/, а не с текущим файлом: ошибки нет."""
    old = load_yaml(example / "frame.yaml")
    (example / "history" / "frame.v2.yaml").write_bytes((example / "frame.yaml").read_bytes())
    new = copy.deepcopy(old)
    new["version"] = 3
    new["deadline"]["value"] = "позже"
    put_yaml(example / "frame.yaml", new)
    assert check_project(example) == []
    # а если нет ни там, ни там — ошибка
    old["deadline"]["value"] = "иначе"
    put_yaml(example / "history" / "frame.v2.yaml", old)
    assert any("рамка в ответе на В1 не совпадает" in e for e in check_project(example))


def test_frame_comparison_is_by_content_not_by_version_number(example):
    """В событии номера версии нет: рамка сравнивается по содержимому, `version` не участвует."""
    frame = FrameFile.model_validate(load_yaml(example / "frame.yaml"))
    assert frame.version == 2
    text = (example / "frame.yaml").read_bytes().replace(b"version: 2", b"version: 1")
    (example / "frame.yaml").write_bytes(text)
    (example / "history" / "frame.v1.yaml").unlink()
    assert not any("рамка в ответе на В1" in e for e in check_project(example))


# --- файлы выхода примера через write дают те же данные ---


def test_example_outputs_written_through_write_equal_the_example(cli, home, clock):
    clock.advance(24 * 3600)
    root = home / "copy"
    shutil.copytree(FIXTURE, root)
    for kind in KINDS:
        (root / f"{kind}.yaml").unlink()
    shutil.rmtree(root / "history")
    code, out, err = write_in(cli, "copy", fixture_outputs())
    assert code == 0, err
    assert check_project(root) == []
    for kind in KINDS:
        written = load_yaml(root / f"{kind}.yaml")
        expected = load_yaml(FIXTURE / f"{kind}.yaml")
        if kind == "frame":
            assert written.pop("version") == 1 and expected.pop("version") == 2
        for d in (written, expected):
            d.pop("schema_version")
        assert parsed(kind, written) == parsed(kind, expected), kind


def test_fixture_input_helper_roundtrip():
    """Вход из примера и рамка-заготовка тестов проходят модели: заготовки не устарели."""
    assert FrameFile.model_validate({"schema_version": 1, "version": 1, **FRAME})
    assert set(fixture_outputs()) == set(KINDS)


def test_put_helper_writes_through_the_cli(cli, home):
    cli("new", "demo")
    out = put(cli, frame=FRAME)
    assert "frame.yaml" in out


def test_rollback_removes_history_folder_created_by_the_refused_write(cli, home):
    assert cli("new", "demo")[0] == 0
    root = home / "demo"
    plan = {
        "questions": [
            {
                "id": "ВП-1",
                "text": "Сколько стоит?",
                "impact": "высокое",
                "branches": [{"if": "дорого", "then": "в тариф"}, {"if": "дёшево", "then": "отдельно"}],
                "close_cost": {"minutes": 3},
                "where": "сайт",
                "status": "открыт",
            }
        ],
        "estimate": {"minutes": 30},
    }
    put(cli, plan=plan)
    assert not (root / "history").exists()
    before = tree_hashes(root)
    plan["estimate"]["minutes"] = 45  # перезапись: прежний план ляжет в history/ ...
    promises = {
        "promises": [
            {
                "id": "О-1",
                "text": "Цена 199 рублей",
                "capability": "тариф",
                "claims": ["У-1"],  # ... а утверждения У-1 нет: новая ошибка, откат
                "conditions": "Москва",
                "status": "подтверждено",
            }
        ]
    }
    code, _, err = write_in(cli, "demo", {"plan": plan, "promises": promises})
    assert code == 1 and "У-1" in err
    assert tree_hashes(root) == before
    assert not (root / "history").exists()


# --- ошибки в записываемых файлах мешают записи, даже если были до неё ---


def test_old_error_in_a_written_file_blocks_the_write(cli, example):
    """Агент «поправил» варианты, но оставил ссылку на отозванное утверждение: запись отклоняется."""
    assert cli("claim", "revoke", "У-2", "--reason", "неверно", "--project", "example")[0] == 0
    assert any("options.yaml" in e for e in check_project(example))  # ошибка появилась до записи
    options = fixture_outputs()["options"]
    options["recommendation"]["why"] = "Цена подтверждена, рост ожидается у аудитории, которая видит ценность"
    before = tree_hashes(example)
    code, _, err = write_in(cli, "example", {"options": options})
    assert code == 1
    assert "options.yaml, recommendation" in err and "У-2" in err
    assert tree_hashes(example) == before  # ни записи, ни события, ни history/


def test_old_error_in_another_file_is_only_a_warning(cli, example):
    assert cli("claim", "revoke", "У-2", "--reason", "неверно", "--project", "example")[0] == 0
    plan = fixture_outputs()["plan"]
    plan["estimate"]["minutes"] = 41
    code, out, err = write_in(cli, "example", {"plan": plan})
    assert code == 0, err
    assert "прежние ошибки проверки" in out
    # а запись, которая чинит файл, проходит: promises и options переписаны на живое утверждение
    new = fixture_outputs()
    new["options"]["recommendation"]["claims"] = ["У-4", "У-7"]
    new["promises"]["promises"][0]["claims"] = ["У-7"]
    second = {
        "text": "Подписка «Защита» в Москве стоит 199 рублей в месяц, первая неделя бесплатно",
        "type": "факт",
        "subject": "цена подписки «Защита»",
        "period": "2026",
        "region": "RU-MOW",
        "quotes": [
            {
                "source": "И-1",
                "text": "Стоимость подписки в Москве — 199 рублей в месяц. Первые семь дней бесплатно",
                "publisher": "компания «Пример-Аналитика»",
                "publisher_kind": "отраслевое исследование",
                "self_reported": False,
            }
        ],
    }
    path = example / ".tmp"
    path.mkdir(exist_ok=True)
    (path / "c.json").write_bytes(json.dumps(second, ensure_ascii=False).encode("utf-8"))
    assert cli("claim", "add", "--file", str(path / "c.json"), "--project", "example")[0] == 0
    code, _, err = write_in(cli, "example", {"options": new["options"], "promises": new["promises"]})
    assert code == 0, err
    assert check_project(example) == []


# --- сверка рамки В1 берёт последний ответ ---


def _reopen_b1(example, frame_body: dict) -> None:
    """Дописывает в журнал примера второе открытие В1 и ответ на него с заданной рамкой."""
    rows = [json.loads(line) for line in (example / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    opened = "2026-11-06T10:20:00+03:00"
    answered = "2026-11-06T10:21:00+03:00"
    base = {"schema_version": 1, "actor": "CLI", "kind": "ворота", "step": "2б"}
    gate = {"gate": "В1", "mode": "автопилот", "questions": 1, "opened_at": opened}
    rows.append({**base, "time": opened, "gate": gate})
    done = {**gate, "answered_at": answered, "answer": "иначе", "frame": frame_body}
    rows.append({**base, "time": answered, "gate": done})
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (example / "journal.jsonl").write_bytes(text.encode("utf-8"))


def test_frame_check_uses_the_last_b1_answer_not_the_first(example):
    """Первый ответ на В1 совпадает с рамкой, последний — нет: ошибка."""
    other = fixture_outputs()["frame"]
    other["deadline"] = {"value": "никогда не записанная рамка", "origin": "допущение"}
    _reopen_b1(example, other)
    found = [e for e in check_project(example) if "рамка в ответе на В1 не совпадает" in e]
    assert found and "строка 11" in found[0]  # строка последнего ответа, а не первого (строка 2)


def test_frame_check_accepts_a_last_answer_that_matches(example):
    _reopen_b1(example, fixture_outputs()["frame"])
    assert not [e for e in check_project(example) if "рамка в ответе на В1" in e]


# --- версия рамки в history/ уже занята ---


def test_taken_frame_version_in_history_is_not_overwritten(cli, example):
    taken = example / "history" / "frame.v2.yaml"
    taken.write_bytes("schema_version: 1\n# занято\n".encode())
    frame = fixture_outputs()["frame"]
    frame["deadline"] = {"value": "к пятнице", "origin": "ответ человека"}
    before = tree_hashes(example)
    code, _, err = write_in(cli, "example", {"frame": frame})
    assert code == 1
    assert "frame.v2.yaml" in err and "разошлись" in err
    assert tree_hashes(example) == before
