"""Признаки части А: доработка по ревью. Каждый тест падает без своей правки кода.

Те же приёмы, что в `test_rubric_checks.py`: эталон `tests/fixtures/project_complete/`, одна испорченная
копия на сценарий, «нет» с ожидаемым сообщением. Тесты без сети.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from helpers import COMPLETE, edit_text, edit_yaml
from strateg import rubric, rubric_checks
from strateg.rubric_checks import Ctx, is_empty_body, list_items, refs

RUBRIC = rubric.load_rubric()


@pytest.fixture
def proj(tmp_path) -> Path:
    shutil.copytree(COMPLETE, tmp_path / "p")
    return tmp_path / "p"


def run(root: Path, step: str = "7") -> dict[str, rubric_checks.Result]:
    return {r.id: r for r in rubric.run_checks(root, RUBRIC, step)}


def msg(root: Path, aid: str, step: str = "7") -> str:
    res = run(root, step)[aid]
    assert not res.ok, f"{aid} должен быть «нет»"
    return " | ".join(res.problems)


def set_section(root: Path, rel: str, name: str, body: str) -> None:
    """Подменяет тело раздела `## name` документа."""
    path = root / rel
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"(## {re.escape(name)}\n).*?(?=\n## |\Z)", re.S)
    assert pattern.search(text), f"в {rel} нет раздела {name}"
    path.write_text(
        pattern.sub(lambda m: m.group(1) + body.strip("\n") + "\n", text, count=1),
        encoding="utf-8",
        newline="\n",
    )


# --- несколько номеров в одних скобках -----------------------------------------------


@pytest.mark.parametrize("mark", ["[О-2, О-1]", "[О-2; О-1]", "[О-1,О-2]", "[О-1 и О-2]", "[О‑2]", "[О–2]"])
def test_a20_unconfirmed_promise_inside_a_multi_number_bracket(proj, mark):
    edit_text(
        proj, "outputs/one-pager.md", "Сообщение о цене [О-1].", f"Защита останавливает любые звонки {mark}."
    )
    text = msg(proj, "А-20")
    assert "one-pager.md" in text and "[О-2]" in text and "не подтверждено" in text


def test_a20_multi_number_bracket_in_the_comms_strategy_too(proj):
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]; обещание", "[О-2, О-1]; обещание")
    assert "comms-strategy.md" in msg(proj, "А-20")


def test_a19_valid_multi_number_bracket_passes_and_unknown_number_inside_is_named(proj):
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]; обещание", "[О-1, О-2]; обещание")
    assert run(proj)["А-19"].ok
    edit_text(proj, "outputs/comms-strategy.md", "[О-1, О-2]", "[О-1, О-9]")
    assert "[О-9]" in msg(proj, "А-19") and "не ведёт" in msg(proj, "А-19")


@pytest.mark.parametrize("bad", ["[О2]", "[О 2]", "[О-1, О2]"])
def test_a19_names_a_bracket_of_unparseable_form(proj, bad):
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]; обещание", f"[О-1] {bad}; обещание")
    text = msg(proj, "А-19")
    assert f"«{bad}»" in text and "неразборной формы" in text


def test_a19_unparseable_bracket_in_the_one_pager_is_named_too(proj):
    edit_text(proj, "outputs/one-pager.md", "Сообщение о цене [О-1].", "Сообщение о цене [О 1].")
    assert "one-pager.md" in msg(proj, "А-19") and "[О 1]" in msg(proj, "А-19")


def test_ordinary_brackets_are_not_promise_marks(proj):
    edit_text(
        proj,
        "outputs/one-pager.md",
        "Сообщение о цене [О-1].",
        "Сообщение о цене [О-1] [см. таблицу] [Оценка] [x].",
    )
    assert run(proj)["А-19"].ok and run(proj)["А-20"].ok


# --- отозванное и заменённое утверждение не подтверждает -----------------------------


@pytest.fixture
def revoked(proj, cli, clock):
    clock.advance(7200)  # журнал эталона кончается позже стартовых часов теста
    code, _, err = cli("claim", "revoke", "У-2", "--reason", "тест", "--project", str(proj))
    assert code == 0, err
    return proj


def test_a8_revoked_claim_is_no(revoked):
    text = msg(revoked, "А-8")
    assert "У-2" in text and "отозвано" in text and "замените ссылку" in text


def test_a10_revoked_claim_is_no(revoked):
    text = msg(revoked, "А-10")
    assert "сообщение 1" in text and "У-2 отозвано" in text and "замените ссылку" in text


def test_a6_counts_only_active_claims(proj, cli, clock):
    clock.advance(7200)
    code, _, err = cli("claim", "revoke", "У-3", "--reason", "тест", "--project", str(proj))
    assert code == 0, err
    text = msg(proj, "А-6")
    assert "ВП-2" in text and "действующего" in text and "У-3 отозвано" in text


def test_replaced_claim_is_no_and_names_the_replacement(proj):
    # У-1 заменено на У-2: ссылка на У-1 в «Известно» уже не годится
    edit_text(proj, "outputs/situation.md", "(У-2, факт)", "(У-1, факт)")
    assert "У-1 заменено на У-2" in msg(proj, "А-8")
    edit_text(proj, "outputs/comms-strategy.md", "подтверждение У-2 (факт)", "подтверждение У-1 (факт)")
    assert "У-1 заменено на У-2" in msg(proj, "А-10")


def test_active_claims_still_pass(proj):
    results = run(proj)
    assert results["А-6"].ok and results["А-8"].ok and results["А-10"].ok


# --- А-25, А-17, А-16 ----------------------------------------------------------------


def test_a25_fact_whose_quotes_are_all_about_the_company_itself(proj):
    """Факт в рекомендации есть, но все его цитаты — заявление компании о себе: «нет»."""

    def self_only(d):
        for c in d["claims"]:
            if c["id"] == "У-2":
                for q in c["quotes"]:
                    q["self_reported"] = True

    edit_yaml(proj, "claims.yaml", self_only)
    assert run(proj)["А-25"].ok is False
    assert "self_reported: false" in msg(proj, "А-25")


def test_a25_independent_fact_passes(proj):
    assert run(proj)["А-25"].ok


def test_a17_row_outside_rows(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["undesired_effects"][0].update(row="ПП-99"))
    text = msg(proj, "А-17")
    assert "ПП-99" in text and "нет среди rows" in text


def _with_premortem(proj, fn) -> list[str]:
    """А-16 на уровне функции: модель собрана без проверки схемы, которая отказала бы раньше."""
    ctx = Ctx(proj, "7")
    ver, errs = ctx.verification()
    assert ver is not None, errs
    changed = ver.model_copy(update={"premortem": fn(list(ver.premortem))})
    ctx.verification = lambda: (changed, [])  # type: ignore[method-assign]
    return rubric_checks.CHECKS["premortem_defensive"](ctx)


def test_a16_number_of_reasons_is_checked_at_function_level(proj):
    assert _with_premortem(proj, lambda m: m) == []
    two = _with_premortem(proj, lambda m: m[:2])
    assert any("причин 2, нужно ровно три" in p for p in two)
    four = _with_premortem(proj, lambda m: [*m, m[0]])
    assert any("причин 4, нужно ровно три" in p for p in four)


def test_a16_same_row_in_two_reasons(proj):
    problems = _with_premortem(proj, lambda m: [m[0], m[0].model_copy(), m[2]])
    assert any("одинаковая строка" in p for p in problems)


# --- пустое тело -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "- <текущая практика>\n- <чужой продукт>",
        "<а>\n\n<б>",
        "—",
        "- —\n- –",
        "...",
        "TODO",
        "todo\ntbd",
        "нет",
        "Нет.",
        "- <роль> TODO",
        "",
        "  \n",
    ],
)
def test_empty_body_has_no_letters_or_digits_outside_placeholders(body):
    assert is_empty_body(body), repr(body)


@pytest.mark.parametrize(
    "body",
    [
        "Нет, потому что цена не зависит от канала.",
        "- <текущая практика> 199 рублей",
        "а",
        "0",
        "TODO: проверить цену",
    ],
)
def test_body_with_a_word_or_digit_is_filled(body):
    assert not is_empty_body(body), repr(body)


@pytest.mark.parametrize("body", ["- <текущая практика>\n- <чужой продукт>", "—", "TODO", "нет"])
def test_a18_section_of_stubs_is_empty(proj, body):
    set_section(proj, "outputs/product-strategy.md", "Альтернативы", body)
    assert "раздел «Альтернативы» пуст" in msg(proj, "А-18")


@pytest.mark.parametrize("role", ["<роль канала>", ".", "TODO", "—", "нет"])
def test_a10_channel_role_made_of_stub_is_empty(proj, role):
    set_section(
        proj,
        "outputs/comms-strategy.md",
        "Роли каналов",
        f"- Личный кабинет: {role}\n- Рассылка семьям — объясняет ценность в первую неделю (У-3).",
    )
    text = msg(proj, "А-10")
    assert "канал 1" in text and "не названа роль" in text


# --- рыхлые списки и маркеры -------------------------------------------------------------


def test_loose_list_keeps_the_indented_paragraph_with_its_item():
    items = list_items("1. Сообщение один\n\n   Подтверждение: У-2\n\n2. Сообщение два (У-2)\n")
    assert len(items) == 2 and "У-2" in items[0] and items[1].endswith("(У-2)")


def test_tab_indent_also_continues_the_item():
    assert list_items("- один\n\n\tпродолжение У-3\n- два") == ["- один продолжение У-3", "- два"]


def test_unindented_paragraph_after_a_blank_line_is_not_part_of_the_list():
    assert list_items("- один\n\nабзац без отступа\n") == ["- один"]


def test_indented_text_after_a_table_does_not_stick_to_the_row():
    items = list_items("| a | b |\n|---|---|\n| У-3 | x |\n\n  хвост\n")
    assert items == ["| У-3 | x |"]


@pytest.mark.parametrize("marker", ["•", "–", "—", "*", "+"])
def test_bullet_markers_start_items(marker):
    assert list_items(f"{marker} А (У-2)\n{marker} Б без ссылки\n") == [
        f"{marker} А (У-2)",
        f"{marker} Б без ссылки",
    ]


def test_a10_loose_list_with_the_claim_in_the_nested_paragraph(proj):
    set_section(
        proj,
        "outputs/comms-strategy.md",
        "Ключевые сообщения",
        "1. Сообщение о цене [О-1]\n\n   Подтверждение: У-2 (факт)\n\n2. Второе сообщение (У-2)\n",
    )
    assert run(proj)["А-10"].ok


def test_a8_item_without_claim_is_not_glued_to_its_neighbour_under_bullets(proj):
    set_section(
        proj,
        "outputs/situation.md",
        "Известно",
        "• Подписка «Защита» в Москве стоит 199 рублей в месяц (У-2, факт)\n• Доля отключений небольшая",
    )
    text = msg(proj, "А-8")
    assert "пункт 2" in text and "нет номера утверждения" in text


def test_a10_channel_role_from_a_dash_bullet(proj):
    set_section(
        proj,
        "outputs/comms-strategy.md",
        "Роли каналов",
        "– Личный кабинет — показывает цену (У-2)\n– Рассылка семьям",
    )
    assert "канал 2" in msg(proj, "А-10")


# --- пробел «источники спорят» назван в тексте пробела ----------------------------------


def _dispute(proj, gap_text: str, item: str) -> None:
    def gap(d):
        d["gaps"][1].update(reason="источники спорят", text=gap_text)

    def link(d):
        for c in d["claims"]:
            if c["id"] == "У-4":
                c["dispute"] = {"claim": "У-2", "reason": "период"}

    edit_yaml(proj, "gaps.yaml", gap)
    edit_yaml(proj, "claims.yaml", link)
    set_section(proj, "outputs/situation.md", "Где источники спорят", item)


def test_a9_gap_is_named_when_the_pair_is_in_the_gap_text(proj):
    _dispute(proj, "У-2 и У-4 расходятся по периоду", "- У-2 и У-4: разные периоды, расхождение записано.")
    assert run(proj)["А-9"].ok


def test_a9_gap_is_not_named_when_neither_the_number_nor_the_pair_is_there(proj):
    _dispute(
        proj, "Конверсия неизвестна: источники спорят", "- У-2 и У-4: разные периоды, расхождение записано."
    )
    text = msg(proj, "А-9")
    assert "П-2" in text and "источники спорят" in text


def test_a9_gap_with_only_one_number_of_the_pair_in_its_text_is_not_named(proj):
    _dispute(proj, "По У-2 данные расходятся", "- У-2 и У-4: разные периоды, расхождение записано.")
    assert "П-2" in msg(proj, "А-9")


# --- номера с другим дефисом и не по форме -----------------------------------------------


def test_a21_non_breaking_hyphen_is_read_as_a_hyphen(proj):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", "## Диагноз\nСм. У‑99.\n")
    assert "У-99 нет в claims.yaml" in msg(proj, "А-21")


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "−"])
def test_a21_every_dash_variant_is_normalised(proj, dash):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", f"## Диагноз\nСм. У{dash}99.\n")
    assert "У-99 нет в claims.yaml" in msg(proj, "А-21")


@pytest.mark.parametrize(
    ("written", "fixed"), [("У99", "У-99"), ("У- 99", "У-99"), ("У -99", "У-99"), ("ВП2", "ВП-2")]
)
def test_a21_number_not_in_the_form_is_reported(proj, written, fixed):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", f"## Диагноз\nСм. {written}.\n")
    text = msg(proj, "А-21")
    assert f"«{written}»" in text and "не по форме" in text and fixed in text


def test_a21_review_scenario_three_bad_spellings_at_once(proj):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", "## Диагноз\nСм. У‑99, У- 99, У99.\n")
    text = msg(proj, "А-21")
    assert "У-99 нет в claims.yaml" in text and "«У- 99»" in text and "«У99»" in text


def test_a21_correct_numbers_in_other_dashes_still_resolve(proj):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", "## Диагноз\nСм. У‑2, У–4 и ВП—1.\n")
    assert run(proj)["А-21"].ok


def test_a21_ordinary_text_is_not_a_sloppy_number(proj):
    edit_text(
        proj,
        "outputs/product-strategy.md",
        "## Диагноз\n",
        "## Диагноз\nИ 5 семей, у 2 из них, П 2 раза, в 2025–2026, Wi-Fi 6, H2O, Y2K, ВПН 1.\n",
    )
    assert run(proj)["А-21"].ok


def test_a8_reads_a_reference_with_a_non_breaking_hyphen(proj):
    edit_text(proj, "outputs/situation.md", "(У-2, факт)", "(У‑2, факт)")
    assert run(proj)["А-8"].ok


def test_refs_normalise_dashes_before_digits_only():
    assert refs("У‑3, У—4, У-5", "У") == ["У-3", "У-4", "У-5"]
    assert refs("У — 3", "У") == []


# --- список слов области: кириллица и частые варианты --------------------------------------


@pytest.mark.parametrize(
    "text",
    ["СИМ-карты", "сим-картой", "Новая e-SIM", "eSIM", "SMS-рассылка", "СМС-рассылки", "смс", "SMS"],
)
def test_a22_variants_of_the_area_words_need_a_ground(tmp_path, text):
    root = tmp_path / "p"
    shutil.copytree(COMPLETE, root)
    for rel in ("inputs/report.txt", "sources/i-1.txt"):
        (root / rel).write_text("Курс программирования для школьников.", encoding="utf-8")
    edit_yaml(root, "frame.yaml", lambda d: d["decision"].update(value="Как продавать курс школьникам"))
    # чистые документы: слов «абонент» и «тариф» эталона нет, остаётся один повод
    for path in (root / "outputs").glob("*.md"):
        body = path.read_text(encoding="utf-8")
        body = re.sub(r"абонент\w*", "клиент", body, flags=re.I)
        body = re.sub(r"тариф\w*", "цена", body, flags=re.I)
        path.write_text(body, encoding="utf-8", newline="\n")
    assert run(root)["А-22"].ok, msg(root, "А-22")
    edit_text(root, "outputs/one-pager.md", "## Кратко\n", f"## Кратко\nНужна {text}.\n")
    assert not run(root)["А-22"].ok
