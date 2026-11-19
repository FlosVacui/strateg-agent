"""`strateg data check`: профиль таблицы — пропуски, единицы, периоды, дубликаты (С-8, ДВ-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import journal_of, tree_hashes
from strateg import tables
from strateg.project_check import check_project

CLEAN_MONTHS = "Месяц;Выручка, тыс. ₽;Заказы, шт.\n" + "\n".join(
    f"2025-{m:02d};{100 + m};{10 * m}" for m in range(1, 7)
)

# Корпус: (имя, таблица, подстроки предупреждений; пусто — замечаний нет). Данных людей и компаний нет.
CORPUS = [
    ("чистый_ряд_месяцев", CLEAN_MONTHS, []),
    (
        "пропуски",
        "Месяц;Выручка, ₽\n2025-01;100\n2025-02;\n2025-03;300\n2025-04;",
        ["пропусков: 2 из 4"],
    ),
    (
        "смешение_единиц_в_значениях",
        "Позиция;Цена\nа;100 ₽\nб;200 $\nв;300 ₽",
        ["смешение единиц в столбце"],
    ),
    (
        "единица_заголовка_против_значений",
        "Месяц;Сумма, млн ₽\n2025-01;5 тыс. ₽\n2025-02;6 тыс. ₽",
        ["смешение единиц"],
    ),
    (
        "часть_значений_без_единицы",
        "Позиция;Доля\nа;15%\nб;20\nв;30%",
        ["у части значений есть единица"],
    ),
    (
        "дыра_в_месяцах",
        "Месяц;Выручка, ₽\n2025-01;1\n2025-02;2\n2025-04;4\n2025-05;5",
        ["дыры в ряду (месяц): нет 2025-03"],
    ),
    (
        "повтор_периода",
        "Месяц;Выручка, ₽\n2025-01;1\n2025-02;2\n2025-02;3\n2025-03;4",
        ["повторы периодов: 1"],
    ),
    (
        "дубликаты_строк",
        "Позиция;Количество, шт.\nа;1\nб;2\nа;1\nв;3",
        ["дубликаты строк: 1 лишних"],
    ),
    (
        "дыра_в_кварталах",
        "Квартал;Выручка, ₽\n2024 Q1;1\n2024 Q2;2\n2024 Q4;4",
        ["дыры в ряду (квартал): нет 2024 Q3"],
    ),
    (
        "дни_с_дырой",
        "Дата;Заказы, шт.\n01.03.2025;5\n02.03.2025;6\n04.03.2025;7\n05.03.2025;8",
        ["дыры в ряду (день): нет 2025-03-03"],
    ),
    (
        "текст_в_числовом_столбце",
        "Позиция;Количество, шт.\nа;1\nб;2\nв;3\nг;4\nд;около десяти",
        ["не числа (например «около десяти»)"],
    ),
    (
        "чистые_кварталы_и_проценты",
        "Квартал;Доля, %;Время, мин\n1 кв. 2024;12,5;30\n2 кв. 2024;13,0;31\n"
        "3 кв. 2024;14,5;29\n4 кв. 2024;15;28",
        [],
    ),
    (
        "заглушки_пропусков_это_пропуски",
        "Месяц;Доля, %\n2025-01;15\n2025-02;н/д\n2025-03;17\n2025-04;нет данных\n2025-05;-\n2025-06;18",
        ["пропусков: 3 из 6"],
    ),
    (
        "смешанные_форматы_дат",
        "Дата;Сумма, ₽\n2025-01-05;1\n05.02.2025;2\n3/3/2025;3\n5 января 2025;4\n2025-04-05;5",
        ["смешанные форматы дат"],
    ),
    (
        "римские_кварталы_с_дырой",
        "Период;Выручка, ₽\nI кв. 2025;1\nII кв. 2025;2\nIV кв. 2025;4",
        ["дыры в ряду (квартал): нет 2025 Q3"],
    ),
    (
        "короткий_дневной_ряд_с_дырой",
        "Дата;Значение, шт.\n2025-01-01;1\n2025-01-02;2\n2025-01-04;3",
        ["дыры в ряду (день): нет 2025-01-03"],
    ),
    (
        "ведущие_нули",
        "Код;Имя\n00123;а\n00456;б\n00789;в",
        ["ведущие нули"],
    ),
    (
        "чистые_слова_похожие_на_единицы",
        "Статья;Чистая прибыль;Население, тыс. чел.;Количество, чел.;Секция;Процесс;Европа;Минимальный чек\n"
        "а;100 ₽;5;3;x;y;z;w\nб;200 ₽;6;4;x;y;z;w",
        [],
    ),
    (
        "широкая_таблица_с_годами_в_шапке",
        "Показатель;2022;2023;2024\nВыручка;1;2;3\nРасход;4;5;6",
        [],
    ),
    (
        "название_над_таблицей",
        "Показатели за 2025;;\nМесяц;Сумма, ₽;Заказы, шт.\n2025-01;1;2\n2025-02;3;4\n2025-03;5;6",
        [],
    ),
    (
        "годы_с_дырой",
        "Год;Выручка, млн ₽\n2021;5\n2022;6\n2024;8",
        ["дыры в ряду (год): нет 2023"],
    ),
]


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def add_table(cli, demo: Path, name: str, text: str) -> str:
    (demo / "inputs").mkdir(exist_ok=True)
    (demo / "inputs" / f"{name}.csv").write_text(text, encoding="utf-8")
    code, out, err = cli("add", f"{name}.csv", "--json")
    assert code == 0, err
    return json.loads(out)["source"]


def profile(cli, target: str) -> tuple[list[str], dict]:
    code, out, err = cli("data", "check", target, "--json")
    assert code == 0, err
    data = json.loads(out)
    return data["warnings"], data


@pytest.mark.parametrize(("name", "text", "expected"), CORPUS, ids=[c[0] for c in CORPUS])
def test_corpus_finds_the_problem_and_stays_silent_on_clean_tables(cli, demo, name, text, expected):
    sid = add_table(cli, demo, name, text)
    warnings, _ = profile(cli, sid)
    if not expected:
        assert warnings == []
    for part in expected:
        assert any(part in w for w in warnings), (part, warnings)
    if expected:
        assert len(warnings) <= 3


def test_columns_carry_type_unit_period_and_missing_share(cli, demo):
    sid = add_table(cli, demo, "ряд", CLEAN_MONTHS)
    _, data = profile(cli, sid)
    cols = {c["name"]: c for c in data["sheets"][0]["columns"]}
    assert cols["Месяц"]["type"] == "дата"
    assert cols["Месяц"]["period"] == {
        "from": "2025-01",
        "to": "2025-06",
        "step": "месяц",
        "gaps": [],
        "duplicates": 0,
        "distinct": 6,
    }
    assert cols["Выручка, тыс. ₽"]["type"] == "число" and cols["Выручка, тыс. ₽"]["unit"] == "тыс. ₽"
    assert cols["Заказы, шт."]["unit"] == "шт."
    assert cols["Заказы, шт."]["missing"] == 0 and cols["Заказы, шт."]["missing_share"] == 0


def test_missing_share_is_a_fraction_of_rows(cli, demo):
    sid = add_table(cli, demo, "пр", "Месяц;Выручка, ₽\n2025-01;1\n2025-02;\n2025-03;3\n2025-04;")
    _, data = profile(cli, sid)
    col = data["sheets"][0]["columns"][1]
    assert col["missing"] == 2 and col["missing_share"] == 0.5


def test_file_in_inputs_works_like_the_source_number(cli, demo):
    add_table(cli, demo, "ряд", CLEAN_MONTHS)
    by_file, _ = profile(cli, "ряд.csv")
    by_id, _ = profile(cli, "И-1")
    assert by_file == by_id == []


def test_text_output_names_columns_and_warnings(cli, demo):
    sid = add_table(cli, demo, "п", "Месяц;Выручка, ₽\n2025-01;1\n2025-03;3")
    code, out, _ = cli("data", "check", sid)
    assert code == 0
    assert "«Выручка, ₽»" in out and "дыры в ряду" in out and "единица ₽" in out


def test_warnings_do_not_change_the_exit_code_and_the_project_stays_valid(cli, demo):
    sid = add_table(cli, demo, "п", "Месяц;Выручка, ₽\n2025-01;1\n2025-03;\n2025-03;")
    assert cli("data", "check", sid)[0] == 0
    assert check_project(demo) == []


def test_only_the_journal_changes(cli, demo):
    sid = add_table(cli, demo, "ряд", CLEAN_MONTHS)
    before = tree_hashes(demo)
    assert cli("data", "check", sid)[0] == 0
    after = tree_hashes(demo)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"journal.jsonl", "state.yaml"}
    event = journal_of(demo)[-1]
    assert event["actor"] == "CLI" and event["name"] == "strateg data check" and sid in event["message"]
    assert check_project(demo) == []


def test_repeated_checks_do_not_break_the_journal(cli, demo):
    sid = add_table(cli, demo, "ряд", CLEAN_MONTHS)
    for _ in range(3):
        assert cli("data", "check", sid)[0] == 0
    assert check_project(demo) == []


def test_not_a_table_and_unknown_inputs_are_refused(cli, demo):
    (demo / "inputs" / "заметка.txt").write_text("просто текст о рынке", encoding="utf-8")
    assert cli("add", "заметка.txt")[0] == 0
    code, _, err = cli("data", "check", "И-1")
    assert code == 1 and "не таблица" in err
    assert cli("data", "check", "И-9")[0] == 1
    code, _, err = cli("data", "check", "нет.csv")
    assert code == 1 and "не зарегистрирован" in err


def test_personal_data_table_is_read_only_through_the_anonymized_copy(cli, demo):
    (demo / "inputs").mkdir(exist_ok=True)
    (demo / "inputs" / "люди.csv").write_text(
        "Контакт;Сумма, ₽\nivan.test@example.test;100\nanna.test@example.test;200", encoding="utf-8"
    )
    code, out, _ = cli("add", "люди.csv", "--json")
    assert code == 0 and json.loads(out)["needs_confirmation"] is True
    code, _, err = cli("data", "check", "И-1")  # замены ещё не подтверждены
    assert code == 1 and "не подтвердил" in err
    assert cli("add", "--confirm", "И-1")[0] == 0
    code, out, _ = cli("data", "check", "И-1", "--json")
    assert code == 0
    assert "example.test" not in out  # профиль строится по обезличенной копии


def test_parser_reads_cells_with_separators_inside_values():
    text = "[лист: данные]\nA1: Позиция | B1: Заметка\nA2: а | B2: x | y | C2: z"
    sheet = tables.parse_sheets(text)[0]
    assert sheet.cells[2] == {1: "а", 2: "x | y", 3: "z"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 200 ₽", (1200.0, "₽")),
        ("15%", (15.0, "%")),
        ("3,5 млн", (3.5, "млн")),
        ("12 ГБ", (12.0, "ГБ")),
        ("250 мин", (250.0, "мин")),
        ("40 шт.", (40.0, "шт.")),
        ("7", (7.0, None)),
        ("−3,2", (-3.2, None)),
    ],
)
def test_numbers_with_units(value, expected):
    assert tables.parse_number(value) == expected


@pytest.mark.parametrize("value", ["abc", "2025-01-03", "01.03.2025", "12 abc", ""])
def test_non_numbers(value):
    assert tables.parse_number(value) is None


def test_xlsx_table_is_profiled_too(cli, demo):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Продажи"
    ws.append(["Месяц", "Выручка, ₽"])
    ws.append(["2025-01", 10])
    ws.append(["2025-03", 30])
    wb.save(demo / "inputs" / "продажи.xlsx")
    assert cli("add", "продажи.xlsx")[0] == 0
    warnings, data = profile(cli, "И-1")
    assert data["sheets"][0]["sheet"] == "Продажи"
    assert any("дыры в ряду" in w for w in warnings)


def only_sheet(cli, demo, name, text):
    return profile(cli, add_table(cli, demo, name, text))[1]["sheets"][0]


def test_title_row_above_the_table_is_skipped_and_named(cli, demo):
    sheet = only_sheet(
        cli, demo, "титул", "Показатели за 2025;;\nМесяц;Сумма, ₽;Заказы, шт.\n2025-01;1;2\n2025-02;3;4"
    )
    names = [c["name"] for c in sheet["columns"]]
    assert names == ["Месяц", "Сумма, ₽", "Заказы, шт."] and sheet["header_row"] == 2
    assert sheet["title_rows"] == [1] and sheet["rows"] == 2


def test_years_in_the_header_make_it_a_header(cli, demo):
    sheet = only_sheet(cli, demo, "годы", "Показатель;2022;2023;2024\nВыручка;1;2;3")
    assert sheet["header"] is True
    assert [c["name"] for c in sheet["columns"]] == ["Показатель", "2022", "2023", "2024"]


def test_unit_row_under_the_header_joins_the_names(cli, demo):
    sheet = only_sheet(cli, demo, "подшапка", "Месяц;Сумма;Доля\n;тыс. ₽;%\n2025-01;1;5\n2025-02;2;6")
    cols = {c["name"]: c for c in sheet["columns"]}
    assert cols["Сумма, тыс. ₽"]["unit"] == "тыс. ₽" and cols["Доля, %"]["unit"] == "%"
    assert sheet["rows"] == 2


def test_unsure_header_is_a_warning(cli, demo):
    warnings, data = profile(cli, add_table(cli, demo, "безшапки", "Москва;100;200\nПитер;300;400"))
    assert data["sheets"][0]["header"] is False
    assert any("заголовок не определён" in w for w in warnings)


def test_stubs_are_missing_values_not_non_numbers(cli, demo):
    sheet = only_sheet(
        cli,
        demo,
        "заглушки",
        "Город;Население\nА;1 234,5\nБ;н/д\nВ;нет данных\nГ;-\nД;1 200\nЕ;n/a\nЖ;—\nЗ;12",
    )
    col = sheet["columns"][1]
    assert col["type"] == "число" and col["missing"] == 5 and col["missing_as_text"] == 5
    assert not any("не числа" in w for w in sheet["warnings"])


def test_brackets_and_american_numbers_are_numbers():
    assert tables.parse_number("(1 234)") == (-1234.0, None)
    assert tables.parse_number("$1,234.50") == (1234.5, "$")
    assert tables.parse_number("1,234,567") == (1234567.0, None)
    assert tables.parse_number("1,5") == (1.5, None)
    assert tables.parse_number("12.5%") == (12.5, "%")


@pytest.mark.parametrize(
    ("header", "unit"),
    [
        ("Чистая прибыль", (None, None)),
        ("Население, тыс. чел.", ("тыс.", None)),
        ("Процесс", (None, None)),
        ("Европа", (None, None)),
        ("Секция", (None, None)),
        ("Минимальный чек", (None, None)),
        ("Часть", (None, None)),
        ("Количество, чел.", (None, None)),
        ("Скорость, Мбит/с", (None, None)),
        ("Выручка, тыс. ₽", ("тыс.", "₽")),
        ("Время (мин)", (None, "мин")),
        ("Доля %", (None, "%")),
        ("Время, часов", (None, "ч")),
    ],
)
def test_header_units_are_found_by_word_boundary(header, unit):
    assert tables.header_unit(header) == unit


def test_clean_profit_column_has_no_false_hours_warning(cli, demo):
    warnings, _ = profile(cli, add_table(cli, demo, "прибыль", "Статья;Чистая прибыль\nа;100 ₽\nб;200 ₽"))
    assert warnings == []


@pytest.mark.parametrize(
    ("value", "label", "fmt"),
    [
        ("I кв. 2025", "2025 Q1", "квартал"),
        ("IV квартал 2025", "2025 Q4", "квартал"),
        ("Q1 2025", "2025 Q1", "квартал"),
        ("1 кв. 2025", "2025 Q1", "квартал"),
        ("3/3/2025", "2025-03-03", "д/м/гггг"),
        ("5 января 2025", "2025-01-05", "д месяц гггг"),
    ],
)
def test_more_period_spellings(value, label, fmt):
    found = tables._parse_period(value)
    assert found is not None and found[0].label() == label and found[1] == fmt


def test_check_with_a_changed_file_refuses_to_pretend(cli, demo):
    add_table(cli, demo, "сменили", CLEAN_MONTHS)
    changed = demo / "inputs" / "сменили.csv"
    changed.write_text(CLEAN_MONTHS + "\n2025-07;1;2", encoding="utf-8")
    code, _, err = cli("data", "check", "inputs/сменили.csv")
    assert code == 1 and "не тот" in err


def test_row_of_only_years_is_a_header(cli, demo):
    sheet = only_sheet(cli, demo, "толькогоды", "2022;2023;2024\n1;2;3\n4;5;6")
    assert sheet["header"] is True and [c["name"] for c in sheet["columns"]] == ["2022", "2023", "2024"]
    assert sheet["rows"] == 2


def test_short_daily_series_with_a_hole_keeps_the_day_step(cli, demo):
    sheet = only_sheet(cli, demo, "дни", "Дата;Значение\n2025-01-01;1\n2025-01-02;2\n2025-01-04;3")
    period = sheet["columns"][0]["period"]
    assert period["step"] == "день" and period["from"] == "2025-01-01" and period["gaps"] == ["2025-01-03"]
