"""`strateg calc run` и `calc check`: воспроизводимый расчёт, изоляция скрипта, число утверждения (С-8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from helpers import PUBLISHER, claims_of, journal_of, load_yaml, write_claims
from strateg import calc as calclib
from strateg import calcrun
from strateg.project import CliError, Project
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
    rel = script(
        demo,
        "dice",
        "import datetime, strateg_calc as sc\nsc.emit({'x': datetime.datetime.now().isoformat()})\n",
    )
    assert run(cli, rel)[0] == 0
    code, _, err = cli("calc", "check")
    assert code == 1 and "расчёт dice" in err


def test_script_never_run_is_a_note_not_an_error(cli, demo):
    script(demo, "draft")
    code, out, _ = cli("calc", "check", "--json")
    assert code == 0 and "draft" in out


# --- охрана: слой 1, разбор скрипта до запуска ------

# Каждый случай — тело скрипта и слово из названия отказа. Часть — однострочники из стандартной библиотеки,
# которыми хук аудита обходился без изобретательности (`_winapi`, `sqlite3`, дескриптор вывода).
BYPASSES = [
    (
        "import _winapi\n_winapi.CreateProcess(None, 'cmd.exe /c echo 1', None, None, 0, 0, None, None, 0)",
        "_winapi",
    ),
    (
        "import _winapi\nh = _winapi.CreateFile('x', 0x80000000, 1, 0, 3, 0x80, 0)\n_winapi.ReadFile(h, 100)",
        "_winapi",
    ),
    ("import _winapi\n_winapi.CreateJunction('C:\\\\Windows', 'zzj')", "_winapi"),
    ("import sqlite3\nsqlite3.connect('pwn.db').execute('create table t(x)')", "sqlite3"),
    ("import os\nos.write(1, b'{}')", "модуль os"),
    ("import sys\nsys.modules['os'].system('echo 1')", "модуль sys"),
    ("import gc\ngc.get_objects()", "модуль gc"),
    ("import importlib\nimportlib.import_module('subprocess')", "модуль importlib"),
    ("import json\njson.codecs.sys.modules['os'].system('echo 1')", "атрибут codecs"),
    ("import collections\ncollections._sys.modules", "атрибут _sys"),
    ("import re\nre.functools.sys", "атрибут sys"),
    ("import strateg_calc as sc\nsc.table.__closure__", "__closure__"),
    ("import strateg_calc as sc\nsc.__dict__", "__dict__"),
    ("x = [].__class__.__base__.__subclasses__()", "__class__"),
    ("x = (lambda: 1).__globals__", "__globals__"),
    ("g = (i for i in range(3))\nnext(g)\ng.gi_frame.f_back", "gi_frame"),
    ("try:\n    1 / 0\nexcept ZeroDivisionError as e:\n    e.__traceback__.tb_frame", "__traceback__"),
    ("x = '{0.__globals__}'.format(len)", "строка формата"),
    ("__import__('socket')", "__import__"),
    ("getattr(len, '__self__')", "getattr"),
    ("eval('1 + 1')", "eval"),
    ("exec('import os')", "exec"),
    ("compile('1', 'x', 'eval')", "compile"),
    ("open('.env').read()", "open"),
    ("open('outputs/calc/x.txt', 'w').write('1')", "open"),
    ("import socket", "модуль socket"),
    ("import urllib.request", "модуль urllib.request"),
    ("import subprocess", "модуль subprocess"),
    ("import ctypes", "модуль ctypes"),
    ("import os\nos.system('echo 1')", "модуль os"),
    ("from pathlib import Path\nPath('.').read_text()", "модуль pathlib"),
    ("import random", "модуль random"),
    ("from . import x", "относительный"),
    ("from json import _default_decoder", "_default_decoder"),
    ("import builtins", "модуль builtins"),
    ("import strateg_calc as sc\nvars(sc)", "vars"),
    ("import strateg_calc as sc\nglobals()", "globals"),
    ("input()", "input"),
    ("class A:\n    def __init__(self):\n        pass", "__init__"),
]


@pytest.mark.parametrize(("body", "word"), BYPASSES, ids=[f"{i:02d}" for i in range(len(BYPASSES))])
def test_script_is_refused_before_it_starts(cli, demo, body, word):
    rel = script(demo, "bad", "import strateg_calc as sc\n" + body + "\nsc.emit({'x': 1})\n")
    before = journal_of(demo)
    code, out, err = run(cli, rel)
    assert code == 1, out
    assert "отклонён до запуска" in out and word in out, out
    assert not (demo / "outputs/calc/bad.json").exists()
    assert not (demo / "pwn.db").exists() and not (demo / "zzj").exists()
    assert journal_of(demo) == before


def test_syntax_error_names_the_line(cli, demo):
    code, out, _ = run(cli, script(demo, "oops", "import strateg_calc as sc\nx = '\n"))
    assert code == 1 and "синтаксическая ошибка в строке 2 скрипта" in out
    assert "calcrun" not in out


def test_allowed_scripts_pass_the_parse():
    text = (
        "from __future__ import annotations\n"
        "import math, statistics as st\nfrom decimal import Decimal\nimport strateg_calc as sc\n"
        "def f(x):\n    return round(math.sqrt(x), 2)\n"
        "if __name__ == '__main__':\n    sc.emit({'x': f(4), 'm': st.mean([1, 2]), 'd': str(Decimal(2))})\n"
    )
    assert calcrun.check_script(text) == []


# --- охрана: слой 2, хук аудита без разбора (что останется, если разбор обойдут) ------


def run_unguarded(root: Path, name: str, timeout: int = 20):
    return calclib.execute(root, Project.load(root).sources, name, timeout, guard=False)


HOOK_CASES = [
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
    ("import sqlite3", "sqlite3"),
    ("import _winapi", "_winapi"),
]


@pytest.mark.parametrize(("body", "word"), HOOK_CASES, ids=[f"{i:02d}" for i in range(len(HOOK_CASES))])
def test_hook_refuses_even_without_the_parse(cli, demo, body, word):
    script(demo, "bad", "import strateg_calc as sc\n" + body + "\nsc.emit({'x': 1})\n")
    before = journal_of(demo)
    with pytest.raises(CliError) as exc:
        run_unguarded(demo, "bad")
    assert word in exc.value.message + " ".join(exc.value.details), exc.value.details
    assert not (demo / "outputs/calc/bad.json").exists()
    assert journal_of(demo) == before


def test_winapi_and_sqlite_are_gone_from_the_process(cli, demo, tmp_path):
    marker = tmp_path / "pwned.txt"
    for name, body in (
        (
            "w",
            f"import _winapi\n_winapi.CreateProcess(None, r'cmd /c echo 1 > {marker}', 0, 0, 0, 0, 0, 0, 0)",
        ),
        ("q", f"import sqlite3\nsqlite3.connect(r'{marker}').execute('create table t(x)')"),
        ("m", "import msvcrt"),
    ):
        script(demo, name, "import strateg_calc as sc\n" + body + "\nsc.emit({'x': 1})\n")
        with pytest.raises(CliError):
            run_unguarded(demo, name)
        assert not marker.exists(), name


def test_descriptors_1_and_2_do_not_reach_the_parent(cli, demo):
    body = (
        "import os, time, strateg_calc as sc\n"
        "t = time.time()\nn = 0\n"
        "while time.time() - t < 1:\n    n += os.write(1, b'x' * 100000)\n"
        "os.write(2, b'err')\nsc.emit({'ok': 1})\n"
    )
    script(demo, "fd", body)
    run = run_unguarded(demo, "fd")
    assert run.values == {"ok": 1}


def test_descriptor_cannot_forge_the_reply(cli, demo):
    reply = '{"status": 0, "stdout": "{}", "stderr": "", "used": {}, "violations": []}'
    body = f"import os, strateg_calc as sc\nos.write(1, {reply!r}.encode())\nos._exit(0)\n"
    script(demo, "forge", body)
    with pytest.raises(CliError) as exc:
        run_unguarded(demo, "forge")
    assert "не читается" in exc.value.message


def test_caught_violation_still_refuses_the_calc(cli, demo):
    body = "import strateg_calc as sc\ntry:\n    open('.env')\nexcept OSError:\n    pass\nsc.emit({'x': 1})\n"
    script(demo, "sly", body)
    with pytest.raises(CliError) as exc:
        run_unguarded(demo, "sly")
    assert "закрытому" in exc.value.message


def test_environment_has_no_keys(cli, demo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-значение")
    monkeypatch.setenv("STRATEG_TEST_SECRET", "секрет")
    body = (
        "import os, strateg_calc as sc\n"
        "sc.emit({'names': sorted(k for k in os.environ if 'KEY' in k or 'SECRET' in k)})\n"
    )
    script(demo, "env", body)
    assert run_unguarded(demo, "env").values == {"names": []}


def test_cwd_is_the_project_root(cli, demo):
    body = "import os, strateg_calc as sc\nsc.emit({'cwd': os.path.basename(os.getcwd())})\n"
    script(demo, "cwd", body)
    assert run_unguarded(demo, "cwd").values == {"cwd": "demo"}


def test_long_script_is_stopped_at_the_named_limit(cli, demo, monkeypatch):
    seen: list = []
    real = calclib.subprocess.run

    def guarded(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real(*args, **{**kwargs, "timeout": min(kwargs.get("timeout") or 30, 30)})

    monkeypatch.setattr(calclib.subprocess, "run", guarded)
    rel = script(demo, "slow", "while True:\n    pass\n")
    code, out, _ = run(cli, rel, "--timeout", "2")
    assert seen == [2], seen  # предел времени передан, а не только «не зависло»
    assert code == 1 and "не уложился в 2 с" in out
    assert not (demo / "outputs/calc/slow.json").exists()
    assert cli("calc", "run", rel, "--timeout", "9999")[0] == 2


def test_huge_output_is_cut(cli, demo):
    code, out, _ = run(cli, script(demo, "loud", "print('x' * 5_000_000)\n"))
    assert code == 1 and "больше 1000000 знаков" in out


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


def test_unknown_unconfirmed_and_synthetic_sources_are_named_in_the_refusal(cli, demo):
    body = "import strateg_calc as sc\nsc.table('И-7')\n"
    code, out, _ = run(cli, script(demo, "nosrc", body))
    assert code == 1 and "И-7" in out and "нет в проекте" in out


def test_personal_data_source_is_closed_until_the_person_confirms(cli, demo):
    (demo / "inputs" / "люди.csv").write_text(
        "Контакт;Сумма, ₽\nivan.test@example.test;100\nanna.test@example.test;200", encoding="utf-8"
    )
    code, out, _ = cli("add", "люди.csv", "--json")
    assert code == 0 and json.loads(out)["needs_confirmation"] is True
    body = "import strateg_calc as sc\nsc.emit({'n': len(sc.table('И-2'))})\n"
    rel = script(demo, "people", body)
    code, out, _ = run(cli, rel)
    assert code == 1 and "И-2" in out and "не подтвердил" in out
    assert not (demo / "outputs/calc/people.json").exists()
    assert cli("add", "--confirm", "И-2")[0] == 0
    code, out, _ = run(cli, rel)
    assert code == 0 and result(demo, "people")["values"] == {"n": 2}
    assert "example.test" not in (demo / "outputs/calc/people.json").read_text(encoding="utf-8")


def test_synthetic_source_is_named_as_such(cli, demo):
    src = Project.load(demo).sources.sources[0]
    why = calclib._unusable(demo, src.model_copy(update={"kind": "синтетика"}))
    assert why is not None and "синтетика" in why and "ничего не доказывает" in why


# --- число утверждения ------


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
    fact = calc_fact("Выручка за квартал составила 600,5 (тыс. ₽) по месяцам")
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 0, out + err
    claim = claims_of(done)[0]
    assert claim["type"] == "факт" and claim["calc"] == {"name": "total", "key": "total"}
    assert check_project(done) == []


def test_claim_matches_through_units_and_rounding(cli, done, tmp_path):
    for text in (
        "Выручка за квартал: 600,5 (тыс. ₽), строк в таблице три",
        "Выручка за квартал — 600.5, а не меньше",
        "Выручка за квартал около 601 (тыс. ₽) (после округления вручную 600,5)",
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
    fact = calc_fact("Выручка за квартал составила 600,5 (тыс. ₽) по месяцам", quote="Заказы за месяц, шт.")
    fact["quotes"][0]["source"] = "И-2"
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "вход И-1 не процитирован" in out + err


def test_fact_by_calc_without_any_quote_is_refused(cli, done, tmp_path):
    fact = calc_fact("Выручка за квартал составила 600,5 (тыс. ₽) по месяцам", quote=None)
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "цитат" in out + err


def test_other_numbers_of_a_calc_fact_still_need_the_quote(cli, done, tmp_path):
    fact = calc_fact("Выручка за квартал составила 600,5 (тыс. ₽), а заказов было 77")
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "числа из текста утверждения" in out + err


def test_assumption_by_calc_checks_the_number(cli, done, tmp_path):
    ok = {
        "text": "Выручка за квартал составит 600,5 (тыс. ₽), если ряд не изменится",
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
            {"text": "Выручка за квартал составит 700 (тыс. ₽), если ряд не изменится"},
            "равного значению расчёта",
        ),
        ({"calc": {"name": "нет", "key": "total"}}, "расчёта нет"),
        ({"calc": {"name": "total", "key": "пропал"}}, "нет ключа"),
        ({"calc": {"name": "total", "key": "by"}}, "не число"),
    ],
)
def test_claim_with_wrong_calc_link_is_refused(cli, done, tmp_path, patch, word):
    bad = {
        "text": "Выручка за квартал составит 600,5 (тыс. ₽), если ряд не изменится",
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
        "text": "Выручка за квартал вырастет, ведь сейчас она 600,5 (тыс. ₽)",
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
        "text": "Выручка за квартал составит 600,5 (тыс. ₽), если ряд не изменится",
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
        "text": "Выручка за квартал составит 600,5 (тыс. ₽), если ряд не изменится",
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


# --- факт по расчёту без входов; прямое чтение ------

ASSUMPTION = {
    "text": "Выручка за квартал составит 600,5 (тыс. ₽), если ряд не изменится",
    "type": "допущение",
    "origin": "агент",
    "subject": "выручка за квартал",
    "taken_from": "расчёт total по таблице И-1",
    "calc": {"name": "total", "key": "total"},
}


def test_fact_by_calc_without_inputs_is_refused_but_assumption_is_fine(cli, demo, tmp_path):
    assert run(cli, script(demo, "fake", "import strateg_calc as sc\nsc.emit({'growth': 0.153})\n"))[0] == 0
    assert result(demo, "fake")["inputs"] == []
    fact = calc_fact(
        "Рост рынка составил 15,3 % за год", key="growth", name="fake", quote="Выручка за месяц, тыс. ₽"
    )
    code, out, err = add_claim(cli, tmp_path, fact)
    assert code == 1 and "без входов" in out + err
    assert claims_of(demo) == []
    ok = {**ASSUMPTION, "text": "Рост рынка составит 15,3 % за год", "taken_from": "расчёт fake"}
    ok["calc"] = {"name": "fake", "key": "growth"}
    assert add_claim(cli, tmp_path, ok)[0] == 0
    assert check_project(demo) == []


def test_fact_by_calc_without_inputs_is_an_error_of_check_too(cli, done, tmp_path):
    assert run(cli, script(done, "fake", "import strateg_calc as sc\nsc.emit({'growth': 0.153})\n"))[0] == 0
    fact = calc_fact("Выручка за квартал составила 600,5 (тыс. ₽) по месяцам")
    assert add_claim(cli, tmp_path, fact)[0] == 0
    claims = done / "claims.yaml"
    data = yaml.safe_load(claims.read_text(encoding="utf-8"))
    data["claims"][0]["calc"] = {"name": "fake", "key": "growth"}
    data["claims"][0]["text"] = "Рост рынка составил 15,3 % за год"
    claims.write_bytes(yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    assert any("факт по расчёту без входов" in e for e in check_project(done))


def test_direct_read_of_a_snapshot_still_counts_as_an_input(cli, demo):
    src = Project.load(demo).sources.sources[0]
    body = (
        f"import strateg_calc as sc\nt = open({src.text_path!r}, encoding='utf-8').read()\n"
        "sc.emit({'n': t.count('2025')})\n"
    )
    script(demo, "direct", body)
    assert run(cli, "outputs/calc/direct.py")[0] == 1  # разбор не пускает open
    assert run_unguarded(demo, "direct").inputs == {"И-1": src.text_sha256}


# --- правка результата вручную ------


def test_hand_edited_result_is_an_error_even_after_another_command(cli, done, tmp_path):
    path = done / "outputs/calc/total.json"
    path.write_text(path.read_text(encoding="utf-8").replace("600.5", "999"), encoding="utf-8")
    assert any("изменён вне CLI" in e for e in check_project(done))
    assert cli("check")[0] == 1
    fake = {**ASSUMPTION, "text": "Выручка за квартал составит 999 (тыс. ₽), если ряд не изменится"}
    code, out, err = add_claim(cli, tmp_path, fake)
    assert code == 1
    assert (done / "claims.yaml").read_text(encoding="utf-8").count("999") == 0
    cli("data", "check", "И-1")  # другая команда принимает хеш файла, но журнал помнит правку
    assert any("правили вне CLI" in e for e in check_project(done))
    assert run(cli, "outputs/calc/total.py")[0] == 0
    assert check_project(done) == []


def test_calc_json_without_a_record_of_the_cli_is_an_error(cli, demo):
    (demo / "outputs/calc").mkdir(parents=True, exist_ok=True)
    assert run(cli, script(demo, "total"))[0] == 0
    data = (demo / "outputs/calc/total.json").read_text(encoding="utf-8")
    (demo / "outputs/calc/ghost.json").write_text(data.replace('"total"', '"ghost"', 1), encoding="utf-8")
    assert any("ghost" in e for e in check_project(demo))


# --- отозванное и заменённое утверждение с расчётом ------


def test_revoked_claim_does_not_hold_the_check_after_the_calc_changes(cli, done, tmp_path):
    assert add_claim(cli, tmp_path, ASSUMPTION)[0] == 0
    rel = "outputs/calc/total.py"
    (done / rel).write_text(SCRIPT.replace("sum(", "2 * sum("), encoding="utf-8")
    code, out, _ = run(cli, rel)
    assert code == 0 and "У-1" in json.loads(out)["drifted_claims"]
    assert any("У-1" in e for e in check_project(done))
    code, _, err = cli("claim", "revoke", "У-1", "--reason", "данные обновились")
    assert code == 0, err
    assert check_project(done) == []


def test_run_says_which_claims_stopped_matching(cli, done, tmp_path):
    assert add_claim(cli, tmp_path, ASSUMPTION)[0] == 0
    rel = "outputs/calc/total.py"
    (done / rel).write_text(SCRIPT.replace("sum(", "2 * sum("), encoding="utf-8")
    code, out, _ = cli("calc", "run", rel)
    assert code == 0 and "Внимание: утверждения У-1 перестали совпадать" in out


# --- помощник: пропуски ------


def test_stubs_are_skipped_and_counted_and_text_is_an_error(cli, demo):
    (demo / "inputs" / "доли.csv").write_text(
        "Месяц;Доля, %\n2025-01;10\n2025-02;н/д\n2025-03;20\n2025-04;-", encoding="utf-8"
    )
    assert cli("add", "доли.csv")[0] == 0
    body = (
        "import statistics, strateg_calc as sc\n"
        "t = sc.table('И-2')\nn = t.numbers('Доля, %')\n"
        "sc.emit({'mean': statistics.mean(n), 'n': len(n), 'missing': t.missing('Доля, %'),\n"
        "         'kept': t.numbers('Доля, %', missing='none')})\n"
    )
    assert run(cli, script(demo, "доли", body))[0] == 0
    assert result(demo, "доли")["values"] == {"kept": [10, None, 20, None], "mean": 15, "missing": 2, "n": 2}
    (demo / "inputs" / "грязь.csv").write_text(
        "Месяц;Сумма\n2025-01;10\n2025-02;итого\n2025-03;30", encoding="utf-8"
    )
    assert cli("add", "грязь.csv")[0] == 0
    body = "import strateg_calc as sc\nsc.emit({'x': sc.table('И-3').numbers('Сумма')})\n"
    code, out, _ = run(cli, script(demo, "грязь", body))
    assert code == 1 and "«итого» не число" in out


# --- сверка числа: знак, порядок, доля, год ------

MATCH_NO = [
    ("рост на 5 %", -5),
    ("−5 %", 5),
    ("-5", 5),
    ("+5", -5),
    ("снижение на 5 %", 5),
    ("Выручка 5 млн ₽", 5),
    ("Выручка 5 тыс. ₽", 5),
    ("3 тыс. человек", 3),
    ("Рост 0,153 %", 0.153),
    ("в 2024 году выручка 300", 2024),
    ("0", 0.4),
    ("Выручка 602 ₽", 600.5),
    ("Выручка 6005 ₽", 600.5),
    ("7 %", 0.0751),
]
MATCH_YES = [
    ("рост 15,3 процента", 0.153),
    ("рост 15,3 п.п.", 0.153),
    ("12,4 миллиона рублей", 12_400_000),
    ("3,2 миллиарда", 3.2e9),
    ("2 тысячи человек", 2000),
    ("1,234.5", 1234.5),
    ("15,3 %", 0.153),
    ("15,3 %", 15.3),
    ("12,4 млн", 12_400_000),
    ("снижение на 5 %", -5),
    ("рост на 5 %", 5),
    ("−5 %", -5),
    ("Выручка 7 % от плана", 0.0749),
    ("в 2024 году выручка 301", 301),
    ("1 234,5", 1234.5),
    ("1,234,567", 1234567),
]


@pytest.mark.parametrize(("text", "value"), MATCH_NO)
def test_number_does_not_match(text, value):
    assert not calclib.number_matches(text, value), (text, value)


@pytest.mark.parametrize(("text", "value"), MATCH_YES)
def test_number_matches(text, value):
    assert calclib.number_matches(text, value), (text, value)
