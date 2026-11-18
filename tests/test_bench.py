"""Стенд оценки `evals/run.py`: копия без ответов, ожидание агента, ответы по карточке, цикл вызовов.

`claude` в тестах только подменённый: вызов — функция, которая отдаёт заготовленный JSON.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _load():
    spec = importlib.util.spec_from_file_location("bench_run", ROOT / "evals" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_run"] = mod
    spec.loader.exec_module(mod)
    return mod


run = _load()
CARD = run.load_case("fraud-protection")


# --- заготовки -----------------------------------------------------------------------------------------


def reply(n: int, *, cost: float | None = None, session: str = "s-1", **extra) -> str:
    """Ответ `claude -p --output-format json`: накопительная стоимость растёт с каждым вызовом."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": f"ответ {n}",
        "session_id": session,
        "total_cost_usd": cost if cost is not None else float(n),
        "modelUsage": {"claude-sonnet-5-5": {"costUSD": float(n)}},
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


class Scripted:
    """Подменённый `claude`: отдаёт заготовленные ответы по порядку и помнит, с чем его вызвали."""

    def __init__(self, replies: list) -> None:
        self.replies, self.prompts, self.sessions, self.opts = list(replies), [], [], []

    def __call__(self, prompt, session_id, opts):
        self.prompts.append(prompt)
        self.sessions.append(session_id)
        self.opts.append(opts)
        item = self.replies[len(self.prompts) - 1]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return item
        return 0, item, ""


class FakeCli:
    """Команды `strateg` в копии: статусы по порядку, `check` всегда в порядке."""

    def __init__(self, statuses: list[dict | None]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def json(self, *args):
        if args[0] != "status":
            return None
        item = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return item

    def run(self, *args, **kw):
        return 0, "Проект проверен: ошибок нет.", ""


def status(step="2б", gates=None, waiting=(), paused=None, in_progress=False) -> dict:
    return {
        "step": step,
        "step_in_progress": in_progress,
        "paused": paused,
        "waiting_gates": list(waiting),
        "gates": gates or {"В1": "отвечено"},
        "work_minutes": 1.5,
        "clock_minutes": 2.0,
        "cost_usd": None,
    }


def gate(g: str, n: int = 1) -> dict:
    return status(step=str(n), waiting=[g], gates={g: "ждут ответа", "n": str(n)}, paused="ворота")


RELEASE = status(step="7", gates={"В5": "впереди"})


def make_bench(tmp_path, statuses, replies, *, limits=None, clock=None, mode="автопилот", card=None):
    copy = tmp_path / "copy"
    (copy / "projects").mkdir(parents=True, exist_ok=True)
    invoker = Scripted(replies)
    kw = {"clock": clock} if clock else {}
    bench = run.Bench(
        "fraud-protection",
        card or CARD,
        mode=mode,
        cli=FakeCli(statuses),
        copy=copy,
        invoker=invoker,
        out=tmp_path / "out",
        limits=limits or run.Limits(),
        settings=tmp_path / "settings.json",
        **kw,
    )
    return bench, invoker


def result_of(bench) -> dict:
    return json.loads((bench.out / "result.json").read_text(encoding="utf-8"))


# --- изолированная копия -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    dest = tmp_path_factory.mktemp("copy") / "copy"
    run.build_copy(ROOT, dest)
    return dest


def test_copy_has_only_what_the_agent_needs(built):
    names = {p.name for p in built.iterdir()}
    assert {
        "AGENTS.md",
        "CLAUDE.md",
        ".claude",
        "knowledge",
        "src",
        "schemas",
        "config",
        "pyproject.toml",
        "uv.lock",
    } <= names
    for banned in ("evals", "docs", "tests", "scripts", ".git", ".github", ".venv"):
        assert banned not in names, banned
    assert list((built / "projects").iterdir()) == []  # ни демо, ни примера, ни прошлых прогонов
    assert not any(p.is_symlink() for p in built.rglob("*"))
    assert (built / "README.md").read_text(encoding="utf-8") == run.STUB_README


def test_copy_does_not_point_back_to_the_repository(built):
    assert run.find_leaks(built, [], ROOT) == []  # ни путей репозитория, ни символических ссылок


def test_copy_of_the_real_repository_has_no_case_strings(built):
    phrases = run.leak_phrases("fraud-protection", CARD)
    assert {"Ева", "Семейная безопасность +", "Защита близких"} <= set(phrases)
    assert any("megafon" in p for p in phrases)
    assert run.find_leaks(built, phrases, ROOT) == []


def test_copy_refuses_existing_folder(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(run.StandError):
        run.build_copy(ROOT, tmp_path / "x")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("knowledge/a.md", "см. «Семейная безопасность +» на странице"),
        ("src/x.py", "# защита близких"),  # регистр не важен
        ("AGENTS.md", "moscow.megafon.ru/services/security/eva"),
        ("knowledge/Защита близких.md", "пусто"),  # строка в имени файла
        ("config/y.yaml", "Ева: да"),
    ],
)
def test_leak_of_case_string_is_found(tmp_path, name, text):
    copy = tmp_path / "c"
    (copy / name).parent.mkdir(parents=True)
    (copy / name).write_text(text, encoding="utf-8")
    assert run.find_leaks(copy, run.leak_phrases("fraud-protection", CARD), ROOT)


def test_short_name_is_a_whole_word_only(tmp_path):
    copy = tmp_path / "c"
    copy.mkdir()
    (copy / "a.md").write_text("Евангелие, Левая, шевелить", encoding="utf-8")
    assert run.find_leaks(copy, ["Ева"]) == []


def test_repository_path_and_symlink_are_leaks(tmp_path):
    copy = tmp_path / "c"
    copy.mkdir()
    (copy / "a.toml").write_text(f'path = "{ROOT.as_posix()}/src"', encoding="utf-8")
    assert any("путь к репозиторию" in x for x in run.find_leaks(copy, [], ROOT))
    link = copy / "docs"
    try:
        link.symlink_to(ROOT / "docs", target_is_directory=True)
    except OSError:
        pytest.skip("символические ссылки недоступны")
    assert any("символическая ссылка" in x for x in run.find_leaks(copy, [], ROOT))


def test_leak_in_copy_cancels_the_run_before_any_call(tmp_path, monkeypatch, capsys):
    def planted(repo, dest):
        (dest / "knowledge").mkdir(parents=True)
        (dest / "knowledge" / "a.md").write_text("«Семейная безопасность +»", encoding="utf-8")

    monkeypatch.setattr(run, "build_copy", planted)
    called = []
    code = run.main(
        ["fraud-protection", "--out", str(tmp_path), "--no-sync"],
        invoker=lambda *a: called.append(a) or (0, "{}", ""),
    )
    assert code == run.EXIT_USAGE
    assert called == []
    assert "нашлись строки кейса" in capsys.readouterr().err


def test_copy_works_on_its_own_and_sees_no_repository(tmp_path):
    """Собственное окружение копии (`uv sync`): CLI импортируется из копии, а не из репозитория."""
    dest = tmp_path / "copy"
    run.build_copy(ROOT, dest)
    cli = run.make_cli(dest, sync=True)
    code, out, err = cli.run("doctor")
    assert "Запись в projects/" in out, (out, err)
    probe = subprocess.run(
        [*cli.prefix[:-1], "python", "-c", "import strateg, sys; print(strateg.__file__)"],
        cwd=dest,
        env=cli.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert Path(probe.stdout.strip()).resolve().is_relative_to(dest.resolve()), probe.stdout + probe.stderr


def test_run_settings_turn_memory_off(tmp_path):
    path = tmp_path / "s.json"
    run.write_run_settings(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["autoMemoryEnabled"] is False
    assert any(x.endswith("/CLAUDE.md") for x in data["claudeMdExcludes"])
    assert run.clean_env()["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_child_env_has_no_product_project_variables(monkeypatch):
    monkeypatch.setenv("STRATEG_PROJECT", "x")
    monkeypatch.setenv("STRATEG_PROJECTS_DIR", "y")
    monkeypatch.setenv("VIRTUAL_ENV", "z")
    env = run.clean_env()
    assert not {"STRATEG_PROJECT", "STRATEG_PROJECTS_DIR", "VIRTUAL_ENV"} & set(env)


# --- права и аргументы вызова --------------------------------------------------------------------------


def opts(tmp_path, **kw):
    return run.CallOptions(cwd=tmp_path, env={}, settings=tmp_path / "s.json", timeout_s=60, **kw)


def test_argv_has_explicit_permissions_and_no_bypass(tmp_path):
    argv = run.build_argv("claude", opts(tmp_path, model="sonnet", max_budget_usd=3.0), None)
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert run.FORBIDDEN_FLAG not in argv
    assert argv[argv.index("--permission-mode") + 1] == run.PERMISSION_MODE
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert {"WebSearch", "Read", "Grep", "Glob"} <= set(tools)
    assert "Bash(uv run strateg *)" in tools
    assert "Bash" not in tools and not any("curl" in t for t in tools)  # curl разрешён не был бы и хуком
    assert argv[argv.index("--max-budget-usd") + 1] == "3.00"
    assert "--resume" not in argv
    assert "--bare" not in argv  # хуки и .mcp.json копии нужны: агент проверяется как есть


def test_argv_resume_uses_the_given_session(tmp_path):
    argv = run.build_argv("claude", opts(tmp_path), "abc-123")
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert "--model" not in argv


def test_prompt_goes_through_stdin_not_argv(tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, b"{}", b"")

    prompt = "Как продавать?\nВот ссылка"
    original = subprocess.run
    subprocess.run = fake_run
    try:
        run.claude_process("claude")(prompt, None, opts(tmp_path))
    finally:
        subprocess.run = original
    assert prompt not in seen["argv"]
    assert seen["input"] == prompt.encode("utf-8")
    assert seen["timeout"] == 60


# --- ожидание агента -----------------------------------------------------------------------------------


def test_wait_gate_stop_release_question():
    assert run.classify_wait(status(waiting=["В4"], paused="ворота")) == run.Wait("ворота", "B4")
    assert run.classify_wait(status(paused="остановка", in_progress=True)) == run.Wait("остановка")
    assert run.classify_wait(status(step="7")) == run.Wait("выпуск")
    assert run.classify_wait(status(step="8")) == run.Wait("выпуск")
    assert run.classify_wait(status(step="7", in_progress=True)) == run.Wait(
        "вопрос"
    )  # выпуск идёт, не закрыт
    assert run.classify_wait(status(step="4", in_progress=True)) == run.Wait("вопрос")
    assert run.classify_wait(None) == run.Wait("вопрос")  # проекта ещё нет


def test_gate_wins_over_stop_and_release():
    both = status(step="7", waiting=["В5"], paused="остановка")
    assert run.classify_wait(both) == run.Wait("ворота", "B5")


@pytest.fixture(scope="module")
def project_cli(tmp_path_factory):
    """CLI на копии примера: проект создаётся командой `new --from`."""
    root = tmp_path_factory.mktemp("w")
    (root / "projects").mkdir()
    env = run.clean_env({"PYTHONPATH": str(ROOT / "src"), "STRATEG_PROJECTS_DIR": str(root / "projects")})
    code = "import sys; from strateg.cli import main; sys.exit(main())"
    cli = run.Cli([sys.executable, "-c", code], root, env)
    assert cli.run("new", "w1", "--from", str(FIXTURES / "project_valid"))[0] == 0
    return cli


def test_wait_is_read_from_real_status_on_fixture(project_cli):
    cli = project_cli
    assert run.classify_wait(cli.json("status")) == run.Wait("вопрос")  # пример стоит на шаге 2б, не ждёт
    assert cli.run("step", "start", "4")[0] == 0
    assert cli.run("gate", "open", "В4", "--questions", "2")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("ворота", "B4")
    assert cli.run("gate", "answer", "В4", "--answer", "2")[0] == 0
    assert cli.run("stop", "--reason", "признаки закрытых данных")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("остановка")
    assert cli.run("step", "done", "4")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("вопрос")
    assert cli.run("step", "start", "7")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("вопрос")
    assert cli.run("step", "done", "7")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("выпуск")


def test_release_is_the_closed_step_7_on_fixture(tmp_path):
    env = run.clean_env({"PYTHONPATH": str(ROOT / "src"), "STRATEG_PROJECTS_DIR": str(tmp_path)})
    code = "import sys; from strateg.cli import main; sys.exit(main())"
    cli = run.Cli([sys.executable, "-c", code], tmp_path, env)
    assert cli.run("new", "c1", "--from", str(FIXTURES / "project_complete"))[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("вопрос")  # выпуск начат, шаг 7 не закрыт
    assert cli.run("step", "done", "7")[0] == 0
    assert run.classify_wait(cli.json("status")) == run.Wait("выпуск")


# --- ответы по карточке --------------------------------------------------------------------------------


def sup(mode="автопилот", card=None):
    return run.Supervisor(card or CARD, mode)


def test_b1_answer_comes_from_card_and_carries_the_mode():
    a = sup().answer(run.Wait("ворота", "B1"), None)
    assert not a.off_card
    assert a.source == "gates.B1"
    assert "как продавать защиту от мошенников действующим абонентам" in a.text
    assert "Аудитория: действующие абоненты по всей России." in a.text
    assert a.text.splitlines()[-1] == "автопилот"
    assert "Всегда" not in a.text  # пояснения к ответу в сообщение не попадают
    assert sup("пилот").answer(run.Wait("ворота", "B1"), None).text.splitlines()[-1] == "пилот"


@pytest.mark.parametrize(
    ("gate_key", "text"), [("B2", "ок"), ("B3", "ок, без правок"), ("B5", "да, выпускайте")]
)
def test_other_gates_answer_by_card(gate_key, text):
    assert sup("пилот").answer(run.Wait("ворота", gate_key), None).text == text


def test_b3_notes_that_the_conditional_rule_is_not_applied():
    a = sup("пилот").answer(run.Wait("ворота", "B3"), None)
    assert a.note and "не применено" in a.note


def write_project(tmp_path, options, gaps=None):
    (tmp_path / "options.yaml").write_text(yaml.safe_dump(options, allow_unicode=True), encoding="utf-8")
    if gaps is not None:
        (tmp_path / "gaps.yaml").write_text(yaml.safe_dump(gaps, allow_unicode=True), encoding="utf-8")
    return tmp_path


def opt(n, kind="активный"):
    return {"id": f"Вар-{n}", "kind": kind}


def gap(n, via="открытый источник", usd=0.5, minutes=8, affects=True, status="открыт"):
    return {
        "id": f"П-{n}",
        "affects_choice": affects,
        "status": status,
        "close_via": via,
        "close_cost": {"usd": usd, "minutes": minutes},
    }


B4 = run.Wait("ворота", "B4")


def test_b4_picks_recommended_active_option(tmp_path):
    p = write_project(
        tmp_path,
        {
            "options": [opt(1, "оставить как есть"), opt(2), opt(3), opt(4, "закрыть направление")],
            "recommendation": {"option": "Вар-3"},
        },
        {"gaps": []},
    )
    assert sup().answer(B4, p).text == "3"


def test_b4_recommended_not_active_takes_lowest_active(tmp_path):
    p = write_project(
        tmp_path,
        {
            "options": [opt(1, "оставить как есть"), opt(3), opt(2), opt(4)],
            "recommendation": {"option": "Вар-1"},
        },
        {"gaps": []},
    )
    assert sup().answer(B4, p).text == "2"


def test_b4_no_active_options_takes_lowest_number(tmp_path):
    p = write_project(
        tmp_path,
        {
            "options": [opt(4, "закрыть направление"), opt(2, "оставить как есть")],
            "recommendation": {"option": "Вар-4"},
        },
        {"gaps": []},
    )
    assert sup().answer(B4, p).text == "2"


def test_b4_first_asks_to_close_the_cheapest_numbered_gap_once(tmp_path):
    options = {"options": [opt(1, "оставить как есть"), opt(2)], "recommendation": {"option": "Вар-2"}}
    gaps = {
        "gaps": [
            gap(4),
            gap(3, via="закрытые данные"),  # закрытыми данными не закрыть
            gap(2, usd=2),  # дороже $1
            gap(5, minutes=11),  # дольше 10 минут
            gap(6, affects=False),  # не влияет на выбор
            gap(7, status="закрыт"),
            gap(9),
        ]
    }
    s = sup()
    p = write_project(tmp_path, options, gaps)
    first = s.answer(B4, p)
    assert first.text == "сначала закрой П-4" and not first.off_card
    assert s.gap_closures_requested == 1
    assert s.answer(B4, p).text == "2"  # закрытие запрошено один раз: дальше выбор по правилу


def test_b4_without_readable_options_is_off_card(tmp_path):
    a = sup().answer(B4, tmp_path)
    assert a.off_card and "не знаю" in a.text.lower()


@pytest.mark.parametrize(
    ("reason", "key"),
    [
        ("оценка плана превышает бюджет", "budget_estimate_over"),
        ("бюджет исчерпан", "budget_exhausted"),
        ("источник с признаками закрытых данных", "personal_or_closed_data_found"),
        ("проверка полноты не пройдена", "completeness_failed"),
        ("новые сведения противоречат выбору", "new_facts_contradict_choice"),
        ("пре-мортем бьёт по выбору", "premortem_hits_choice"),
        ("robots.txt запрещает сохранить страницу", "asked_to_save_page_forbidden_by_robots"),
    ],
)
def test_stop_answer_by_card(tmp_path, reason, key):
    (tmp_path / "journal.jsonl").write_text(
        json.dumps(
            {"kind": "действие", "name": "внеплановая остановка", "message": reason}, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    a = sup().answer(run.Wait("остановка"), tmp_path)
    assert a.source == f"off_card.stops.{key}"
    assert a.text == CARD["off_card"]["stops"][key]
    assert not a.off_card


def test_unknown_stop_and_plain_question_are_marked_off_card(tmp_path):
    (tmp_path / "journal.jsonl").write_text(
        json.dumps(
            {"kind": "действие", "name": "внеплановая остановка", "message": "странное"}, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    stop = sup().answer(run.Wait("остановка"), tmp_path)
    question = sup().answer(run.Wait("вопрос"), None, "Какой цвет у страницы?")
    for a in (stop, question):
        assert a.off_card and a.text == run.IDLE_ANSWER and a.source == "вне карточки"


def test_question_about_closed_data_uses_the_card_template():
    a = sup().answer(run.Wait("вопрос"), None, "Какая у вас конверсия в оплату?")
    assert a.text == CARD["unknown_and_closed"]["answer_template"] and not a.off_card
    assert (
        sup().answer(run.Wait("вопрос"), None, "Конверсия посчитана по открытым данным.").off_card
    )  # не вопрос


# --- цикл вызовов --------------------------------------------------------------------------------------


def test_resume_keeps_the_same_session_id(tmp_path):
    statuses = [gate("В1", 1), gate("В4", 4), RELEASE]
    bench, invoker = make_bench(tmp_path, statuses, [reply(1), reply(2), reply(3)])
    assert bench.run("первое сообщение") == run.EXIT_OK
    assert invoker.sessions == [None, "s-1", "s-1"]
    assert invoker.prompts[0] == "первое сообщение"
    assert invoker.prompts[1].startswith("своё:")
    res = result_of(bench)
    assert res["session_ids"] == ["s-1"] and res["outcome"] == "выпуск"


def test_each_call_carries_the_explicit_limits(tmp_path):
    limits = run.Limits(budget_usd=8.0, max_turns=30)
    bench, invoker = make_bench(
        tmp_path, [gate("В1"), RELEASE], [reply(1, cost=1.5), reply(2, cost=3.0)], limits=limits
    )
    bench.run("x")
    assert invoker.opts[0].max_budget_usd == 8.0
    assert invoker.opts[1].max_budget_usd == pytest.approx(6.5)  # остаток: бюджет минус накопленное
    assert all(o.max_turns == 30 for o in invoker.opts)


def test_cost_is_the_last_cumulative_value_not_the_sum(tmp_path):
    bench, _ = make_bench(
        tmp_path,
        [gate("В1", 1), gate("В4", 4), RELEASE],
        [
            reply(1, cost=1.0),
            reply(2, cost=2.5),
            reply(3, cost=4.0, modelUsage={"a": {"costUSD": 3.0}, "sub": {"costUSD": 1.0}}),
        ],
    )
    bench.run("x")
    res = result_of(bench)
    assert res["total_cost_usd"] == 4.0  # не 7.5
    assert res["cumulative_costs"] == [1.0, 2.5, 4.0]
    assert res["model_usage"] == {
        "a": {"costUSD": 3.0},
        "sub": {"costUSD": 1.0},
    }  # с субагентами, последнее значение


def test_missing_cost_keeps_previous_value_and_is_noted(tmp_path):
    no_cost = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": "s-1"}
    )
    bench, _ = make_bench(tmp_path, [gate("В1", 1), RELEASE], [reply(1, cost=2.0), no_cost])
    bench.run("x")
    res = result_of(bench)
    assert res["total_cost_usd"] == 2.0
    assert any("нет стоимости" in n for n in res["notes"])


def test_result_records_gates_stops_questions_and_off_card(tmp_path):
    statuses = [
        gate("В1", 1),
        status(step="2б", paused="остановка", in_progress=True),
        status(step="3", in_progress=True),
        RELEASE,
    ]
    bench, _ = make_bench(tmp_path, statuses, [reply(i) for i in range(1, 5)])
    assert bench.run("x") == run.EXIT_OK
    res = result_of(bench)
    assert [g["gate"] for g in res["gates"]] == ["B1"]
    assert len(res["stops"]) == 1 and len(res["questions"]) == 1
    assert res["off_card_answers"] == 2  # остановка без причины и вопрос без ворот: ответов в карточке нет
    assert {o["wait"] for o in res["off_card"]} == {"остановка", "вопрос"}
    assert res["calls"] == 4 and res["aborted"] is False
    assert dt.datetime.fromisoformat(res["started_at"]).tzinfo is not None  # время с поясом


def test_failure_in_the_middle_writes_partial_results_and_code_1(tmp_path):
    bench, invoker = make_bench(
        tmp_path, [gate("В1", 1), gate("В4", 4), RELEASE], [reply(1), (1, "", "сеть недоступна"), reply(3)]
    )
    assert bench.run("x") == run.EXIT_FAILED
    res = result_of(bench)
    assert res["outcome"] == "сбой" and "код 1" in res["failure"] and "сеть недоступна" in res["failure"]
    assert res["calls"] == 2 and len(invoker.prompts) == 2  # после сбоя прогон не продолжается
    assert [g["gate"] for g in res["gates"]] == ["B1"]  # итоги первого вызова сохранены
    assert res["total_cost_usd"] == 1.0
    assert (bench.out / "raw" / "call-01.json").is_file() and (bench.out / "raw" / "call-02.json").is_file()


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "упал",
                    "session_id": "s-1",
                }
            ),
            "is_error",
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "лимит",
                    "session_id": "s-1",
                }
            ),
            "is_error",
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": False,
                    "result": "",
                    "session_id": "s-1",
                }
            ),
            "error_max_turns",
        ),
        ("не json", "не JSON"),
        (subprocess.TimeoutExpired(["claude"], 1800), "тайм-аут"),
        (FileNotFoundError("claude"), "не запустился"),
    ],
)
def test_call_errors_stop_the_run_with_code_1(tmp_path, bad, needle):
    bench, _ = make_bench(tmp_path, [gate("В1", 1), RELEASE], [reply(1), bad])
    assert bench.run("x") == run.EXIT_FAILED
    res = result_of(bench)
    assert res["outcome"] == "сбой" and needle in res["failure"]
    assert res["aborted"] is True and res["calls"] == 2


def test_call_limit_stops_without_extra_calls(tmp_path):
    statuses = [gate("В1", i) for i in range(1, 9)]
    bench, invoker = make_bench(
        tmp_path, statuses, [reply(i) for i in range(1, 9)], limits=run.Limits(max_calls=3)
    )
    assert bench.run("x") == run.EXIT_LIMIT
    res = result_of(bench)
    assert res["outcome"] == "предел вызовов" and res["calls"] == 3 and len(invoker.prompts) == 3
    assert res["aborted"] is True


def test_budget_limit_stops_the_run(tmp_path):
    statuses = [gate("В1", i) for i in range(1, 9)]
    bench, invoker = make_bench(
        tmp_path, statuses, [reply(i, cost=3.0 * i) for i in range(1, 9)], limits=run.Limits(budget_usd=5.0)
    )
    assert bench.run("x") == run.EXIT_LIMIT
    assert result_of(bench)["outcome"] == "предел бюджета" and len(invoker.prompts) == 2


def test_budget_reached_inside_a_call_is_a_limit_not_a_failure(tmp_path):
    over = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": False,
            "result": "",
            "session_id": "s-1",
            "total_cost_usd": 5.2,
        }
    )
    bench, _ = make_bench(tmp_path, [gate("В1", 1), gate("В4", 4)], [reply(1, cost=1.0), over])
    assert bench.run("x") == run.EXIT_LIMIT
    res = result_of(bench)
    assert res["outcome"] == "предел бюджета" and res["total_cost_usd"] == 5.2


def test_time_limit_stops_the_run(tmp_path):
    ticks = iter(range(0, 100000, 4000))  # каждый запрос времени — плюс 4000 с
    statuses = [gate("В1", i) for i in range(1, 9)]
    base = dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    bench, invoker = make_bench(
        tmp_path,
        statuses,
        [reply(i) for i in range(1, 9)],
        limits=run.Limits(max_minutes=100),
        clock=lambda: base + dt.timedelta(seconds=next(ticks)),
    )
    assert bench.run("x") == run.EXIT_LIMIT
    assert result_of(bench)["outcome"] == "предел времени" and len(invoker.prompts) < 8


def test_run_without_progress_is_stopped_as_a_loop(tmp_path):
    same = gate("В1", 1)
    bench, invoker = make_bench(tmp_path, [same], [reply(i) for i in range(1, 9)])
    assert bench.run("x") == run.EXIT_LIMIT
    res = result_of(bench)
    assert res["outcome"] == "зацикливание" and len(invoker.prompts) == run.MAX_STALLED + 1


def test_materials_go_to_inputs_once_the_project_exists(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "brief.txt").write_text("материал", encoding="utf-8")
    card = {**CARD, "materials": {"files": ["brief.txt"]}}
    bench, _ = make_bench(tmp_path, [gate("В1", 1), RELEASE], [reply(1), reply(2)], card=card)
    bench.case_dir = case_dir
    project = tmp_path / "copy" / "projects" / "p1"
    project.mkdir(parents=True)
    (project / "state.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "copy" / "projects" / ".active").write_text("p1\n", encoding="utf-8")
    bench.run("x")
    assert (project / "inputs" / "brief.txt").read_text(encoding="utf-8") == "материал"


# --- запуск целиком ------------------------------------------------------------------------------------


def test_missing_claude_exits_with_code_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    assert run.main(["fraud-protection", "--out", str(tmp_path)]) == run.EXIT_USAGE
    assert "claude не найден" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []  # ничего не записано


def test_unknown_case_exits_with_code_2(tmp_path, capsys):
    assert run.main(["no-such-case", "--dry-run", "--out", str(tmp_path)]) == run.EXIT_USAGE
    assert "нет карточки" in capsys.readouterr().err


def test_record_is_refused_for_dry_run(tmp_path, capsys):
    before = (ROOT / "docs" / "benchmark.md").read_bytes()
    assert run.main(["fraud-protection", "--dry-run", "--record", "--out", str(tmp_path)]) == run.EXIT_USAGE
    assert (ROOT / "docs" / "benchmark.md").read_bytes() == before


@pytest.mark.parametrize(
    ("mode", "calls", "gates"), [("автопилот", 5, ["B1", "B4"]), ("пилот", 8, ["B1", "B2", "B3", "B4", "B5"])]
)
def test_dry_run_goes_from_the_first_call_to_release(tmp_path, mode, calls, gates):
    code = run.main(["fraud-protection", "--dry-run", "--no-sync", "--mode", mode, "--out", str(tmp_path)])
    assert code == run.EXIT_OK
    (folder,) = list((tmp_path / "fraud-protection").iterdir())
    res = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert res["outcome"] == "выпуск" and res["calls"] == calls and res["dry_run"] is True
    assert [g["gate"] for g in res["gates"]] == gates
    assert [s["source"] for s in res["stops"]] == ["off_card.stops.personal_or_closed_data_found"]
    assert res["off_card_answers"] == 1  # вопрос без ворот: в карточке ответа нет
    assert res["total_cost_usd"] == 0.5 * calls  # накопительная, не сумма
    assert res["session_ids"] == ["dry-run-session"]
    assert res["final"]["check"]["ok"] is True and res["final"]["step"] == "7"
    assert res["final"]["work_minutes"] is not None and res["final"]["clock_minutes"] is not None
    assert res["project_id"] == "dry-run" and (folder / "project" / "state.yaml").is_file()
    assert len(list((folder / "raw").glob("call-*.json"))) == calls


def test_dry_run_answers_reach_the_project_journal(tmp_path):
    run.main(["fraud-protection", "--dry-run", "--no-sync", "--out", str(tmp_path)])
    (folder,) = list((tmp_path / "fraud-protection").iterdir())
    journal = (folder / "project" / "journal.jsonl").read_text(encoding="utf-8")
    assert (
        "автопилот" in journal and "своё: как продавать защиту" in journal
    )  # ответ В1 из карточки записан агентом


def test_dry_run_with_the_copys_own_environment(tmp_path):
    code = run.main(["fraud-protection", "--dry-run", "--out", str(tmp_path)])  # с `uv sync` в копии
    assert code == run.EXIT_OK


# --- запись в docs/benchmark.md ------------------------------------------------------------------------


def sample_result(**kw):
    final = {"work_minutes": 7.5, "check": {"ok": True}, "completeness": {"result": "пройдено"}}
    base = {
        "started_at": "2026-11-18T10:00:00+03:00",
        "case": "fraud-protection",
        "mode": "автопилот",
        "model": None,
        "calls": 5,
        "clock_seconds": 600.0,
        "total_cost_usd": 2.5,
        "gates": [1, 2],
        "stops": [1],
        "off_card": [],
        "outcome": "выпуск",
        "final": final,
    }
    return {**base, **kw}


def test_record_adds_a_row_to_an_existing_table(tmp_path):
    path = tmp_path / "benchmark.md"
    path.write_text(
        "# Замеры\n\n## Прогоны стенда\n\nтекст\n\n"
        + run.RECORD_HEADER
        + "\n"
        + run._sep()
        + "\n\n## Дальше\n\nещё\n",
        encoding="utf-8",
    )
    run.record(path, sample_result())
    run.record(path, sample_result(case="other"))
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [ln for ln in lines if ln.startswith("| 2026-11-18")]
    assert len(rows) == 2 and "2.50" in rows[0] and "пройдено" in rows[0] and "7.5" in rows[0]
    assert lines.index(rows[1]) < lines.index("## Дальше")


def test_record_fits_the_real_benchmark_table(tmp_path):
    path = tmp_path / "benchmark.md"
    path.write_bytes((ROOT / "docs" / "benchmark.md").read_bytes())
    run.record(path, sample_result())
    lines = path.read_text(encoding="utf-8").splitlines()
    row = next(i for i, ln in enumerate(lines) if ln.startswith("| 2026-11-18"))
    assert lines[row - 1] == run._sep() and lines[row - 2] == run.RECORD_HEADER
    assert lines[row + 1] == "" and lines[row + 2] == "## Ограничения"


def test_record_creates_the_section(tmp_path):
    path = tmp_path / "b.md"
    path.write_text("# Замеры\n", encoding="utf-8")
    run.record(path, sample_result(total_cost_usd=None))
    text = path.read_text(encoding="utf-8")
    assert run.RECORD_SECTION in text and "нет данных" in text


def test_main_does_not_touch_card_or_expectations():
    status_out = subprocess.run(
        ["git", "status", "--porcelain", "evals/cases"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert status_out.strip() == ""
