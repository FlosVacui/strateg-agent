"""`strateg guide check`: рубрика гайда интервью, корпус вопросов в обе стороны (ДВ-1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from strateg import guide
from strateg.guide import Question

RUBRIC = guide.load_rubric()
ROOT = Path(__file__).resolve().parent.parent

# Вопросы с явным признаком: проверка называет именно его.
BAD = [
    ("Будете ли вы пользоваться таким приложением для заказа обедов?", "future"),
    ("Купили бы вы подписку на доставку обедов за 199 рублей в месяц?", "future"),
    ("Если бы такой сервис существовал, вы бы его установили?", "future"),
    ("Хотели бы вы получать напоминания о заказе?", "future"),
    ("Планируете ли вы менять способ заказа в следующем году?", "future"),
    ("Не правда ли, что заказывать обеды по телефону неудобно?", "leading"),
    ("Согласитесь, что общий список заказов экономит время?", "leading"),
    ("Разве не удобнее, когда все заказы собраны в одном месте?", "leading"),
    ("Вы ведь тоже ненавидите ждать курьера?", "leading"),
    ("Пользовались ли вы приложениями для заказа обедов?", "closed"),
    ("Есть ли у вас список любимых кафе? Да или нет.", "closed"),
    ("Как вам наш новый сервис заказа обедов?", "pitch"),
    ("Представьте, что есть приложение, которое собирает заказы команды.", "pitch"),
    ("Мы запускаем подписку на обеды: что вы о ней думаете?", "pitch"),
]
# Хорошие вопросы о прошлом: ни ошибок, ни предупреждений.
GOOD = [
    "Расскажите о последнем разе, когда вы заказывали обед в офис.",
    "Что вы делали сразу перед тем, как начать выбирать кафе?",
    "Как вы решали, что заказать на всю команду?",
    "Сколько времени это заняло и во сколько обошлось?",
    "Кто ещё участвовал в заказе и что он делал?",
    "Чем вы пользовались, чтобы собрать пожелания: чатом, таблицей, звонками?",
    "Что в тот раз пошло не так?",
    "Когда вы в последний раз возвращались к этой задаче и почему?",
    "Какие кафе вы рассматривали и почему выбрали это?",
    "Что вы уже пробовали, чтобы сократить время на заказ?",
    "Покажите, где у вас сейчас хранится список любимых кафе.",
    "Как вы узнали об этом способе заказа?",
]


def q(text: str, number: int = 1, follow_up: list[str] | None = None) -> Question:
    return Question(number, text, follow_up or [])


def checks(text: str, level: str | None = None, **kw) -> set[str]:
    found = guide.check_question(q(text, **kw), RUBRIC)
    return {f.check for f in found if level is None or f.level == level}


def test_corpus_has_at_least_twenty_questions_in_both_directions():
    assert len(BAD) + len(GOOD) >= 20 and len(BAD) >= 10 and len(GOOD) >= 10


@pytest.mark.parametrize(("text", "check"), BAD)
def test_bad_question_is_an_error_of_its_kind(text, check):
    assert check in checks(text, "ошибка"), text


@pytest.mark.parametrize("text", GOOD)
def test_good_question_has_no_findings(text):
    assert checks(text) == set(), text


def test_closed_question_with_a_follow_up_is_allowed():
    assert "closed" in checks("Пользовались ли вы приложениями для заказа обедов?")
    sub = checks("Пользовались ли вы приложениями для заказа?", follow_up=["Если да: каким и чем кончилось?"])
    assert "closed" not in sub
    second = checks("Пользовались ли вы приложениями для заказа? Если да, расскажите, как это было.")
    assert "closed" not in second


def test_doubtful_signs_are_warnings_not_errors():
    assert checks("Что бы вы сделали в такой ситуации?", "ошибка") == set()
    assert "future" in checks("Что бы вы сделали в такой ситуации?", "предупреждение")
    assert "opinion" in checks("Как вы думаете, почему заказ занимает так много времени?", "предупреждение")
    assert "closed" in checks("Вы заказывали обеды на прошлой неделе?", "предупреждение")


def test_open_word_first_keeps_the_particle_from_being_closed():
    assert "closed" not in checks("Расскажите, пользовались ли вы приложениями и что из этого вышло.")


# --- гайд целиком ------------------------------------------------------------------------------------


def make_guide(questions: list[str], extra: str = "") -> str:
    body = "\n".join(f"{n}. {t}" for n, t in enumerate(questions, start=1))
    return (
        "# Гайд интервью\n\n## Кратко\nо\n\n## Цель\nц\n\n## Кого спрашиваем\nк\n\n"
        f"## Вопросы о прошлом опыте\n{body}\n\n## Что слушаем\nс\n\n## Чего не спрашиваем\n"
        f"1. Будете ли вы пользоваться? Согласитесь, что удобно?\n{extra}"
    )


def test_a_good_guide_passes_and_examples_of_forbidden_questions_are_not_counted():
    report = guide.check_guide(make_guide(GOOD[:10]), RUBRIC)
    assert report.errors == [] and len(report.guide.questions) == 10
    assert report.warnings == []


@pytest.mark.parametrize("count", [3, 7, 13, 20])
def test_question_count_outside_eight_to_twelve_is_an_error(count):
    questions = (GOOD * 2)[:count]
    report = guide.check_guide(make_guide(questions), RUBRIC)
    assert [f.check for f in report.errors] == ["question_count"], count


@pytest.mark.parametrize("count", [8, 12])
def test_question_count_limits_are_inclusive(count):
    assert guide.check_guide(make_guide(GOOD[:count]), RUBRIC).errors == []


def test_missing_sections_and_past_anchor_are_warnings():
    text = "## Вопросы\n" + "\n".join(f"{n}. Что вы делали на шаге {n}?" for n in range(1, 9))
    report = guide.check_guide(text, RUBRIC)
    assert report.errors == []
    kinds = {f.check for f in report.warnings}
    assert {"sections", "past_anchor"} <= kinds


def test_violation_names_the_question_number_and_the_reason(tmp_path, capsys):
    questions = list(GOOD[:9]) + ["Будете ли вы пользоваться таким приложением?"]
    path = tmp_path / "guide.md"
    path.write_bytes(make_guide(questions).encode("utf-8"))
    from strateg.cli import main

    assert main(["guide", "check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "вопрос 10: ошибка Г-2: вопрос о будущем" in out and "будете" in out
    assert "Г-Б1" in out  # смысловая часть — для критика или агента
    assert main(["guide", "check", str(path), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["questions"] == 10
    assert data["errors"][0]["question"] == 10 and data["errors"][0]["check"] == "future"
    assert [x["id"] for x in data["semantic"]][0] == "Г-Б1"


def test_clean_guide_exits_zero_and_warnings_do_not_change_the_code(tmp_path, capsys):
    from strateg.cli import main

    path = tmp_path / "guide.md"
    path.write_bytes(make_guide(GOOD[:10]).encode("utf-8"))
    assert main(["guide", "check", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    questions = [*GOOD[:9], "Как вы думаете, почему это так долго?"]
    path.write_bytes(make_guide(questions).encode("utf-8"))
    assert main(["guide", "check", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"] and data["errors"] == []


def test_guide_without_questions_missing_file_and_bad_encoding(tmp_path, capsys):
    from strateg.cli import main

    empty = tmp_path / "e.md"
    empty.write_bytes("# Гайд\n\nтекст\n".encode())
    assert main(["guide", "check", str(empty)]) == 1 and "ни одного вопроса" in capsys.readouterr().err
    assert main(["guide", "check", str(tmp_path / "нет.md")]) == 1
    bad = tmp_path / "b.md"
    bad.write_bytes("1. вопрос".encode("cp1251"))
    assert main(["guide", "check", str(bad)]) == 1 and "UTF-8" in capsys.readouterr().err


def test_bulleted_questions_and_wrapped_lines_are_parsed():
    text = "## Вопросы\n- Что вы делали?\n  Расскажите подробнее.\n- Когда это было в последний раз?\n"
    g = guide.parse_guide(text)
    assert [x.text for x in g.questions] == ["Что вы делали?", "Когда это было в последний раз?"]
    assert g.questions[0].follow_up == ["Расскажите подробнее."]


# --- рубрика и шаблон --------------------------------------------------------------------------------


def test_rubric_file_is_quoted_yaml_and_every_check_has_a_rule():
    path = ROOT / "knowledge" / "rubrics" / "interview-guide.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert [x["check"] for x in data["part_a"]] == list(guide._CHECKS)
    ids = [x["id"] for x in data["part_a"] + data["part_b"]]
    assert len(ids) == len(set(ids)) and len(data["part_b"]) <= 15 and len(data["part_a"]) <= 30
    assert (data["min_questions"], data["max_questions"]) == (8, 12)
    for item in data["part_a"]:
        assert item["rule"] and item["source"]


def test_rubric_patterns_do_not_flag_the_template_wording():
    """Описания в шаблоне гайда — не вопросы: рубрика не должна ловить сам шаблон как плохой гайд."""
    text = (ROOT / "knowledge" / "templates" / "interview-guide.md").read_text(encoding="utf-8")
    assert guide.parse_guide(text).questions == []
