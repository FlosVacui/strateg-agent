"""Сквозной путь по файлам выхода: от `new` до замены утверждения, только командами CLI."""

from __future__ import annotations

import json

import yaml

from helpers import FRAME, URL, fact, fixture_outputs, journal_of, load_yaml, put, write_claims
from strateg.project_check import check_project

# Номера утверждений в примере проекта (У-2…У-6) -> номера в этом проекте (У-1…У-5)
REMAP = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5}


def claims_file(tmp_path) -> str:
    items = [
        fact(plan_question="ВП-1"),
        {
            "text": "Подключают защиту чаще всего семьи с пожилыми родителями",
            "type": "гипотеза",
            "subject": "кто подключает защиту",
            "period": "2026",
            "basis": ["У-1"],
            "check_method": "спросить в интервью с пятью семьями",
            "plan_question": "ВП-2",
        },
        {
            "text": "За год доля абонентов с защитой вырастет до 15–17 %",
            "type": "прогноз",
            "subject": "доля абонентов с защитой",
            "period": "2027",
            "basis": ["У-1"],
            "forecast_range": {"low": 15, "high": 17, "unit": "% абонентов"},
        },
        {
            "text": "Часть абонентов отключит защиту из-за ложных срабатываний",
            "type": "гипотеза",
            "subject": "отключение защиты",
            "period": "2027",
            "basis": ["У-1"],
            "check_method": "выгрузка отключений за первый месяц",
        },
        {
            "text": "Семьи не начнут пользоваться защитой без объяснения в первую неделю",
            "type": "гипотеза",
            "subject": "использование защиты в первую неделю",
            "period": "2027",
            "basis": ["У-2"],
            "check_method": "события использования услуги в первую неделю",
        },
    ]
    return write_claims(tmp_path, items)


def test_output_files_from_new_to_replaced_claim(cli, home, net, page_html, tmp_path):
    root = home / "demo"
    data = fixture_outputs(REMAP)
    net.add(URL, page_html)

    assert cli("new", "demo")[0] == 0
    assert cli("step", "start", "1")[0] == 0
    # рамка: черновик до ответа на В1, правка черновика не создаёт версий
    put(cli, frame=FRAME)
    assert cli("gate", "open", "В1", "--questions", "2")[0] == 0
    put(cli, frame={**FRAME, "deadline": {"value": "к пятнице", "origin": "ответ человека"}})
    assert load_yaml(root / "frame.yaml")["version"] == 1
    assert not (root / "history").exists()
    assert cli("gate", "answer", "В1", "--answer", "1а, ок")[0] == 0
    assert cli("step", "done", "1")[0] == 0
    assert check_project(root) == []

    # источник и утверждения
    assert cli("step", "start", "2б")[0] == 0
    assert cli("fetch", URL, "--region", "RU-MOW")[0] == 0
    code, out, err = cli("claim", "add", "--file", claims_file(tmp_path))
    assert code == 0, err

    # план; потом пробелы и варианты одним входом: они ссылаются друг на друга
    plan = data["plan"]
    plan["questions"][2].pop("gap")
    plan["questions"][2]["status"] = "открыт"
    code, _, err = cli("write", "--file", "-", stdin=json.dumps({"plan": plan}, ensure_ascii=False))
    assert code == 0, err
    options = data["options"]
    chosen = options.pop("chosen")
    code, _, err = cli(
        "write",
        "--file",
        "-",
        stdin=json.dumps({"gaps": data["gaps"], "options": options}, ensure_ascii=False),
    )
    assert code == 0, err
    assert cli("step", "done", "2б")[0] == 0

    # В4: выбор человека попадает в options.yaml, прежний файл остаётся в history/
    assert cli("step", "start", "4")[0] == 0
    assert cli("gate", "open", "В4", "--questions", "1")[0] == 0
    assert cli("gate", "answer", "В4", "--answer", "Вар-2, ок")[0] == 0
    put(cli, options={**options, "chosen": chosen})
    assert (root / "history" / "options.v1.yaml").is_file()
    assert load_yaml(root / "options.yaml")["chosen"] == "Вар-2"

    # обещания, план проверки, решения
    put(cli, promises=data["promises"], verification=data["verification"], decisions=data["decisions"])
    assert cli("step", "done", "4")[0] == 0
    assert cli("check")[0] == 0
    assert check_project(root) == []

    # замена утверждения: рекомендация и обещание пока на заменённом, их переписывает write
    second = fact(
        "Стоимость подписки в Москве — 199 рублей в месяц. Первые семь дней после подключения "
        "услуга предоставляется бесплатно",
        text="Подписка «Защита» в Москве стоит 199 рублей в месяц, первая неделя бесплатно",
        plan_question="ВП-1",
    )
    code, out, err = cli("claim", "replace", "У-1", "--file", write_claims(tmp_path, second, "second.yaml"))
    assert code == 0, err
    assert "У-6" in out and "рекомендация" in out and "обещание О-1" in out
    assert cli("check")[0] == 1  # пока места использования не переписаны

    options["recommendation"]["claims"] = ["У-6", "У-3"]
    promises = data["promises"]
    promises["promises"][0]["claims"] = ["У-6"]
    put(cli, options={**options, "chosen": chosen}, promises=promises)
    code, out, err = cli("check")
    assert code == 0, err
    assert check_project(root) == []

    # всё записано так, как должно быть: файлы, версии, журнал
    for name in ("frame", "plan", "gaps", "options", "promises", "verification", "decisions"):
        assert (root / f"{name}.yaml").is_file(), name
    assert sorted(p.name for p in (root / "history").iterdir()) == [
        "options.v1.yaml",
        "options.v2.yaml",
        "promises.v1.yaml",
    ]
    state = yaml.safe_load((root / "state.yaml").read_text(encoding="utf-8"))
    assert state["gates"]["В1"] == "отвечено" and state["gates"]["В4"] == "отвечено"
    names = [e.get("name") for e in journal_of(root)]
    assert names.count("strateg write options") == 3
    assert "strateg claim replace" in names
    # рамка в событии ответа на В1 — та, что лежит в файле
    answer = next(e for e in journal_of(root) if e["kind"] == "ворота" and e["gate"].get("frame"))
    assert answer["gate"]["frame"]["deadline"]["value"] == "к пятнице"
    assert load_yaml(root / "frame.yaml")["deadline"]["value"] == "к пятнице"
    # а сводка после всего этого не находит ни «прервано», ни правок вне агента
    code, out, _ = cli("status", "--brief")
    assert code == 0 and "Прервано" not in out and "Изменено вне агента" not in out
    # ни одного «изменено вне агента»: правок руками не было, собственные записи CLI ложных событий не дают
    assert [e for e in journal_of(root) if e["kind"] == "изменено вне агента"] == []
