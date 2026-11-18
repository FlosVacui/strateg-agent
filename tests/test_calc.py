"""`strateg calc run` и `calc check`: воспроизводимый расчёт, изоляция скрипта, число утверждения (С-8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from helpers import PUBLISHER, claims_of, journal_of, load_yaml, write_claims
from strateg import calc as calclib
from strateg.project_check import check_project

TABLE = "Месяц;Выручка за месяц, тыс. ₽\n2025-01;100\n2025-02;200\n2025-03;300.5"
SCRIPT = """import strateg_calc as sc

t = sc.table("И-1")
total = sum(t.numbers("Выручка за месяц, тыс. ₽"))
sc.emit({"total": total, "rows": len(t), "by": {"first": 100}})
"""


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    root = home / "demo"
    (root / "inputs" / "продажи.csv").write_text(TABLE, encoding="utf-8")
    assert cli("add", "продажи.csv")[0] == 0
    return root


def script(root: Path, name: str, text: str = SCRIPT) -> str:
    folder = root / "outputs" / "calc"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(text, encoding="utf-8")
    return f"outputs/calc/{name}.py"


def run(cli, rel: str, *extra: str):
    return cli("calc", "run", rel, "--json", *extra)


def result(root: Path, name: str) -> dict:
    return json.loads((root / "outputs" / "calc" / f"{name}.json").read_text(encoding="utf-8"))


def test_run_writes_values_hashes_time_and_python(cli, demo):
    code, out, err = run(cli, script(demo, "total"))
    assert code == 0, err
    data = result(demo, "total")
    assert data["values"] == {"by": {"first": 100}, "rows": 3, "total": 600.5}
    assert data["name"] == "total" and data["python"].count(".") == 2 and data["run_at"].endswith("+03:00")
    assert len(data["script"]["sha256"]) == 64
    src = load_yaml(demo / "sources.yaml")["sources"][0]
    assert data["inputs"] == [
        {
            "source": "И-1",
            "path": src["text_path"],
            "sha256": src["text_sha256"],
        }
    ]
    event = journal_of(demo)[-1]
    assert event["actor"] == "CLI" and event["name"] == "strateg calc run" and "total" in event["message"]
    assert check_project(demo) == []


def test_repeat_gives_the_same_and_calc_check_passes(cli, demo):
    rel = script(demo, "total")
    assert run(cli, rel)[0] == 0
    first = (demo / "outputs/calc/total.json").read_bytes()
    assert run(cli, rel)[0] == 0
    assert result(demo, "total")["values"] == json.loads(first)["values"]
    code, out, err = cli("calc", "check", "--json")
    assert code == 0, err
    assert json.loads(out)["checked"] == ["total"]
    assert check_project(demo) == []


def test_cyrillic_name_and_set_order_do_not_change_the_result(cli, demo):
    rel = script(
        demo,
        "итог",
        "import strateg_calc as sc\nsc.emit({'order': list({'б', 'а', 'в', 'г'})})\n",
    )
    assert run(cli, rel)[0] == 0
    first = result(demo, "итог")["values"]
    assert run(cli, rel)[0] == 0
    assert result(demo, "итог")["values"] == first
    assert cli("calc", "check")[0] == 0


def test_changed_script_is_an_error_for_check_and_calc_check(cli, demo):
    rel = script(demo, "total")
    assert run(cli, rel)[0] == 0
    (demo / rel).write_text(SCRIPT + "\n# правка\n", encoding="utf-8")
    errors = check_project(demo)
    assert any("расчёт total" in e and "скрипт" in e and "изменён" in e for e in errors)
    code, _, err = cli("calc", "check")
    assert code == 1 and "расчёт total" in err
    assert cli("check")[0] == 1


def test_changed_input_is_an_error(cli, demo):
    rel = script(demo, "total")
    assert run(cli, rel)[0] == 0
    src = demo / "sources.yaml"
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    text_file = demo / data["sources"][0]["text_path"]
    text_file.write_bytes(text_file.read_bytes().replace(b"300.5", b"400"))
    import hashlib

    data["sources"][0]["text_sha256"] = hashlib.sha256(text_file.read_bytes()).hexdigest()
    src.write_bytes(yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    errors = check_project(demo)
    assert any("расчёт total" in e and "вход И-1 изменился" in e for e in errors)
    code, _, err = cli("calc", "check")
    assert code == 1 and "расчёт total" in err


def test_forged_result_is_caught_by_calc_check_and_noted_as_outside_change(cli, demo):
    rel = script(demo, "total")
    assert run(cli, rel)[0] == 0
    path = demo / "outputs/calc/total.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["values"]["total"] = 999
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code, _, err = cli("calc", "check")
    assert code == 1 and "расчёт total" in err and "не совпали" in err
    assert cli("data", "check", "И-1")[0] == 0
    events = journal_of(demo)
    assert any(
        e["kind"] == "изменено вне агента" and e.get("path") == "outputs/calc/total.json" for e in events
    )


def test_nondeterministic_script_fails_calc_check(cli, demo):
    rel = script(demo, "dice", "import random, strateg_calc as sc\nsc.emit({'x': random.random()})\n")
    assert run(cli, rel)[0] == 0
    code, _, err = cli("calc", "check")
    assert code == 1 and "расчёт dice" in err


def test_script_never_run_is_a_note_not_an_error(cli, demo):
    script(demo, "draft")
    code, out, _ = cli("calc", "check", "--json")
    assert code == 0 and "draft" in out


@pytest.mark.parametrize(
    ("body", "word"),
    [
        ("import socket\nsocket.create_connection(('93.184.216.34', 80), 1)", "socket"),
        ("import urllib.request\nurllib.request.urlopen('http://example.com', timeout=1)", "socket"),
        ("open('.env').read()", "чтение файла"),
        ("open('journal.jsonl').read()", "чтение файла"),
        ("open('sources.yaml').read()", "чтение файла"),
        ("open('../other.txt').read()", "чтение файла"),
        ("open(r'C:\\\\Windows\\\\win.ini').read()", "чтение файла"),
        ("open('outputs/calc/x.txt', 'w').write('1')", "запись в файл"),
        ("import os\nos.remove('claims.yaml')", "os.remove"),
        ("import os\nos.listdir('.')", "просмотр папки"),
        ("import subprocess", "subprocess"),
        ("import ctypes", "ctypes"),
        ("import os\nos.system('echo 1')", "os.system"),
        ("from pathlib import Path\nPath('.').resolve().parent.joinpath('x').read_text()", "чтение файла"),
    ],
)
def test_forbidden_actions_are_refused_and_nothing_is_written(cli, demo, body, word):
    rel = script(demo, "bad", "import strateg_calc as sc\n" + body + "\nsc.emit({'x': 1})\n")
    before = journal_of(demo)
    code, out, err = run(cli, rel)
    assert code == 1, out
    assert word in out
    assert not (demo / "outputs/calc/bad.json").exists()
    assert journal_of(demo) == before


def test_caught_violation_still_refuses_the_calc(cli, demo):
    body = "import strateg_calc as sc\ntry:\n    open('.env')\nexcept OSError:\n    pass\nsc.emit({'x': 1})\n"
    code, out, _ = run(cli, script(demo, "sly", body))
    assert code == 1 and "закрытому" in out


def test_environment_has_no_keys(cli, demo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-значение")
    monkeypatch.setenv("STRATEG_TEST_SECRET", "секрет")
    body = (
        "import os, strateg_calc as sc\n"
        "sc.emit({'names': sorted(k for k in os.environ if 'KEY' in k or 'SECRET' in k)})\n"
    )
    assert run(cli, script(demo, "env", body))[0] == 0
    assert result(demo, "env")["values"] == {"names": []}


def test_cwd_is_the_project_root(cli, demo):
    body = "import os, strateg_calc as sc\nsc.emit({'cwd': os.path.basename(os.getcwd())})\n"
    assert run(cli, script(demo, "cwd", body))[0] == 0
    assert result(demo, "cwd")["values"] == {"cwd": "demo"}


def test_long_script_is_stopped_at_the_named_limit(cli, demo):
    rel = script(demo, "slow", "while True:\n    pass\n")
    code, out, _ = run(cli, rel, "--timeout", "2")
    assert code == 1 and "не уложился в 2 с" in out
    assert not (demo / "outputs/calc/slow.json").exists()
    assert cli("calc", "run", rel, "--timeout", "9999")[0] == 2


def test_huge_output_is_cut(cli, demo):
    code, out, _ = run(cli, script(demo, "loud", "print('x' * 5_000_000)\n"))
    assert code == 1 and "больше" in out


def test_result_must_be_one_json_object(cli, demo):
    assert run(cli, script(demo, "nojson", "print('привет')\n"))[0] == 1
    assert run(cli, script(demo, "lst", "import json\nprint(json.dumps([1, 2]))\n"))[0] == 1
    assert run(cli, script(demo, "boom", "raise ValueError('ломаюсь')\n"))[0] == 1


def test_script_must_lie_in_outputs_calc(cli, demo):
    (demo / "outputs" / "x.py").write_text(SCRIPT, encoding="utf-8")
    code, _, err = cli("calc", "run", "outputs/x.py")
    assert code == 1 and "outputs/calc" in err
    assert cli("calc", "run", "outputs/calc/нет.py")[0] == 1
    rel = script(demo, "Bad Name")
    assert cli("calc", "run", rel)[0] == 2


def test_unconfirmed_and_unknown_sources_are_not_readable(cli, demo):
    body = "import strateg_calc as sc\nsc.table('И-7')\n"
    code, out, _ = run(cli, script(demo, "nosrc", body))
    assert code == 1 and "И-7" in out


# --- число утверждения ------------------------------------------------------------------------------------


def calc_fact(
    text: str, key: str = "total", name: str = "total", quote: str | None = "Выручка за месяц, тыс. ₽"
) -> dict:
    data = {
        "text": text,
        "type": "факт",
        "subject": "выручка за квартал",
        "period": "2025",
        "calc": {"name": name, "key": key},
        "quotes": [{"source": "И-1", "text": quote, "locator": "B1", **PUBLISHER}] if quote else [],
    }
    return data


@pytest.fixture
def done(cli, demo) -> Path:
    assert run(cli, script(demo, "total"))[0] == 0
    return demo


def add_claim(cli, tmp_path, data):
    return cli("claim", "add", "--file", write_claims(tmp_path, data), "--json")


def test_fact_by_calc_with_a_quote_of_every_input_is_recorded(cli, done, tmp_path):
    fact = calc_fact("Выручка за квартал составила 600,5 тыс. ₽ по месяцам")
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 0, out + err
    claim = claims_of(done)[0]
    assert claim["type"] == "факт" and claim["calc"] == {"name": "total", "key": "total"}
    assert check_project(done) == []


def test_claim_matches_through_units_and_rounding(cli, done, tmp_path):
    for text in (
        "Выручка за квартал: 600,5 тыс. ₽, строк в таблице три",
        "Выручка за квартал — 600.5, а не меньше",
        "Выручка за квартал около 601 тыс. ₽ (после округления вручную 600,5)",
    ):
        assert calclib.number_matches(text, 600.5), text
    assert calclib.number_matches("Доля 15,3 % от выручки", 0.153)
    assert calclib.number_matches("Выручка 12,4 млн ₽", 12_400_000)
    assert calclib.number_matches("Выручка 1 234,5 ₽", 1234.5)
    assert calclib.number_matches("Снижение на 5 пунктов", -5)
    assert calclib.number_matches("Выручка 601 ₽", 600.5)  # округление до показанных знаков
    assert not calclib.number_matches("Выручка 602 ₽", 600.5)
    assert not calclib.number_matches("Выручка 6005 ₽", 600.5)
    assert not calclib.number_matches("Выручка без чисел", 600.5)


def test_fact_by_calc_needs_a_quote_of_each_input(cli, done, tmp_path):
    other = "Месяц;Заказы за месяц, шт.\n2025-01;5\n2025-02;6"
    (done / "inputs" / "другая.csv").write_text(other, encoding="utf-8")
    assert cli("add", "другая.csv")[0] == 0
    fact = calc_fact("Выручка за квартал составила 600,5 тыс. ₽ по месяцам", quote="Заказы за месяц, шт.")
    fact["quotes"][0]["source"] = "И-2"
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "вход И-1 не процитирован" in out + err


def test_fact_by_calc_without_any_quote_is_refused(cli, done, tmp_path):
    fact = calc_fact("Выручка за квартал составила 600,5 тыс. ₽ по месяцам", quote=None)
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "цитат" in out + err


def test_other_numbers_of_a_calc_fact_still_need_the_quote(cli, done, tmp_path):
    fact = calc_fact("Выручка за квартал составила 600,5 тыс. ₽, а заказов было 77")
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "числа из текста утверждения" in out + err


def test_assumption_by_calc_checks_the_number(cli, done, tmp_path):
    ok = {
        "text": "Выручка за квартал составит 600,5 тыс. ₽, если ряд не изменится",
        "type": "допущение",
        "origin": "агент",
        "subject": "выручка за квартал",
        "taken_from": "расчёт total по таблице И-1",
        "calc": {"name": "total", "key": "total"},
    }
    code, out, err = add_claim(cli, tmp_path, ok)
    assert code == 0, out + err
    assert claims_of(done)[0]["calc"] == {"name": "total", "key": "total"}
    assert check_project(done) == []


@pytest.mark.parametrize(
    ("patch", "word"),
    [
        (
            {"text": "Выручка за квартал составит 700 тыс. ₽, если ряд не изменится"},
            "равного значению расчёта",
        ),
        ({"calc": {"name": "нет", "key": "total"}}, "расчёта нет"),
        ({"calc": {"name": "total", "key": "пропал"}}, "нет ключа"),
        ({"calc": {"name": "total", "key": "by"}}, "не число"),
    ],
)
def test_claim_with_wrong_calc_link_is_refused(cli, done, tmp_path, patch, word):
    bad = {
        "text": "Выручка за квартал составит 600,5 тыс. ₽, если ряд не изменится",
        "type": "допущение",
        "origin": "агент",
        "subject": "выручка за квартал",
        "taken_from": "расчёт total по таблице И-1",
        "calc": {"name": "total", "key": "total"},
    }
    bad.update(patch)
    before = (done / "claims.yaml").read_bytes()
    code, out, err = add_claim(cli, tmp_path, bad)
    assert code == 1 and word in out + err
    assert (done / "claims.yaml").read_bytes() == before


def test_hypothesis_cannot_carry_a_calc(cli, done, tmp_path):
    bad = {
        "text": "Выручка за квартал вырастет, ведь сейчас она 600,5 тыс. ₽",
        "type": "гипотеза",
        "origin": "агент",
        "subject": "выручка",
        "check_method": "сверить с оплатами за следующий квартал",
        "basis": [],
        "calc": {"name": "total", "key": "total"},
    }
    code, out, err = add_claim(cli, tmp_path, bad)
    assert code == 1 and "calc" in out + err


def test_claim_goes_wrong_when_the_calc_value_changes(cli, done, tmp_path):
    ok = {
        "text": "Выручка за квартал составит 600,5 тыс. ₽, если ряд не изменится",
        "type": "допущение",
        "origin": "агент",
        "subject": "выручка за квартал",
        "taken_from": "расчёт total по таблице И-1",
        "calc": {"name": "total", "key": "total"},
    }
    assert add_claim(cli, tmp_path, ok)[0] == 0
    rel = "outputs/calc/total.py"
    (done / rel).write_text(SCRIPT.replace("sum(", "2 * sum("), encoding="utf-8")
    assert run(cli, rel)[0] == 0
    errors = check_project(done)
    assert any("У-1" in e and "равного значению расчёта" in e for e in errors)


def test_calc_claim_repeat_is_a_duplicate_and_calc_difference_is_named(cli, done, tmp_path):
    ok = {
        "text": "Выручка за квартал составит 600,5 тыс. ₽, если ряд не изменится",
        "type": "допущение",
        "origin": "агент",
        "subject": "выручка за квартал",
        "taken_from": "расчёт total по таблице И-1",
        "calc": {"name": "total", "key": "total"},
    }
    assert add_claim(cli, tmp_path, ok)[0] == 0
    code, out, _ = add_claim(cli, tmp_path, ok)
    assert code == 0 and json.loads(out)["claims"][0]["duplicate"] is True
    other = {**ok, "calc": {"name": "total", "key": "rows"}}
    code, out, err = add_claim(cli, tmp_path, other)
    assert code == 1 and "calc" in out + err
