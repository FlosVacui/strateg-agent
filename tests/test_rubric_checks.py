"""Признаки части А рубрики полноты: эталонная папка, испорченные копии, разбор документов.

Эталон — `tests/fixtures/project_complete/`: на нём все 25 признаков «да». На каждый признак — копия,
в которой испорчено ровно одно место, и ожидаемое сообщение «нет». Тесты без сети.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers import COMPLETE, edit_journal, edit_text, edit_yaml, journal_of, tree_hashes
from strateg import rubric, rubric_checks
from strateg.rubric_checks import (
    CHECKS,
    COMMS_SECTIONS,
    PRODUCT_SECTIONS,
    SEC_CHANNELS,
    SEC_DISPUTES,
    SEC_KNOWN,
    SEC_MESSAGES,
    find_section,
    is_empty_body,
    list_items,
    refs,
    sections,
)

ROOT = Path(__file__).resolve().parent.parent
RUBRIC = rubric.load_rubric()
TEMPLATES = ROOT / "knowledge" / "templates"


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


def only_bad(root: Path, aid: str, step: str = "7") -> str:
    """«нет» ровно у одного признака, и это aid; возвращает его сообщение."""
    bad = [i for i, r in run(root, step).items() if not r.ok]
    assert aid in bad, f"{aid} должен быть «нет»"
    return msg(root, aid, step)


# --- реестр ----------------------------------------------------------------


def test_registry_and_rubric_name_the_same_checks():
    names = [a.cli_check for a in RUBRIC.part_a]
    assert len(names) == len(set(names)) == 25
    assert set(names) == set(CHECKS)


def test_section_names_in_code_are_headings_of_the_templates():
    wanted = {
        "situation.md": [SEC_KNOWN, SEC_DISPUTES],
        "product-strategy.md": list(PRODUCT_SECTIONS),
        "comms-strategy.md": list(COMMS_SECTIONS),
    }
    for file, names in wanted.items():
        titles = {t.strip() for t in sections((TEMPLATES / file).read_text(encoding="utf-8"))}
        for name in names:
            assert name in titles, f"{file}: нет раздела «{name}»"
    assert SEC_MESSAGES in COMMS_SECTIONS and SEC_CHANNELS in COMMS_SECTIONS


# --- эталон ----------------------------------------------------------------


def test_reference_project_passes_all_25():
    results = run(COMPLETE)
    assert len(results) == 25
    assert [r.id for r in results.values() if not r.ok] == []


def test_step_filter_drops_later_steps():
    assert "А-20" not in run(COMPLETE, "6") and len(run(COMPLETE, "6")) == 24
    assert list(run(COMPLETE, "1")) == ["А-2"]
    assert "А-24" not in run(COMPLETE, "4") and "А-13" not in run(COMPLETE, "3")


# --- по одной испорченной копии на признак -----------------------------------


def test_a1_missing_file_names_file_and_step(proj):
    (proj / "verification.yaml").unlink()
    text = only_bad(proj, "А-1")
    assert "нет файла verification.yaml" in text and "шаг 5" in text


def test_a1_unreadable_file_is_reported_not_raised(proj):
    (proj / "gaps.yaml").write_text("gaps: [", encoding="utf-8")
    assert "gaps.yaml" in msg(proj, "А-1")


def test_a2_frame_missing_from_gate_event(proj):
    edit_journal(
        proj, lambda ev: [e["gate"].pop("frame", None) for e in ev if e.get("gate", {}).get("gate") == "В1"]
    )
    assert "gate.frame" in only_bad(proj, "А-2")


def test_a2_frame_file_missing(proj):
    (proj / "frame.yaml").unlink()
    assert "нет файла frame.yaml" in msg(proj, "А-2")


def test_a3_gate_status_against_mode(proj):
    edit_yaml(proj, "state.yaml", lambda d: d["gates"].update({"В2": "впереди"}))
    text = only_bad(proj, "А-3")
    assert "gates.В2" in text and "впереди" in text


def test_a3_pilot_gate_cannot_pass_automatically(proj):
    def pilot(events):
        for e in events:
            if e.get("gate", {}).get("gate") == "В3":
                e["gate"]["mode"] = "пилот"

    edit_journal(proj, pilot)
    assert "gates.В3" in only_bad(proj, "А-3") and "пилот" in msg(proj, "А-3")


def test_a3_missing_gate_event(proj):
    edit_journal(
        proj, lambda ev: ev.__setitem__(slice(None), [e for e in ev if e.get("gate", {}).get("gate") != "В3"])
    )
    assert "нет события ворот В3" in msg(proj, "А-3")


def test_a3_checks_only_gates_due_at_the_step(proj):
    edit_yaml(proj, "state.yaml", lambda d: d["gates"].update({"В4": "впереди"}))
    gates = rubric_checks.CHECKS["gates_marked"]
    assert gates(rubric_checks.Ctx(proj, "4")) == []  # В4 к шагу 4 ещё впереди
    assert gates(rubric_checks.Ctx(proj, "6")) != []


def test_a4_b4_without_answer(proj):
    def drop(events):
        for e in events:
            if e.get("gate", {}).get("gate") == "В4":
                e["gate"].pop("answered_at")
                e["gate"].pop("answer")

    edit_journal(proj, drop)
    assert "В4" in msg(proj, "А-4")


def test_a4_more_than_five_questions_on_b1(proj):
    def many(events):
        for e in events:
            if e.get("gate", {}).get("gate") == "В1":
                e["gate"]["questions"] = 6

    edit_journal(proj, many)
    assert "не больше 5" in msg(proj, "А-4")


def test_a5_open_question(proj):
    edit_yaml(proj, "plan.yaml", lambda d: d["questions"][1].update(status="открыт"))
    assert "ВП-2" in only_bad(proj, "А-5") and "открыт" in msg(proj, "А-5")


def test_a6_closed_question_without_claim(proj):
    def move(d):
        for c in d["claims"]:
            if c["id"] == "У-3":
                c["plan_question"] = "ВП-3"

    edit_yaml(proj, "claims.yaml", move)
    assert "ВП-2" in only_bad(proj, "А-6") and "plan_question" in msg(proj, "А-6")


def test_a7_metric_missing(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["metric_coverage"].pop())
    assert "«качество продукта»" in only_bad(proj, "А-7")


def test_a7_claim_of_wrong_type(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["metric_coverage"][0].update(claims=["У-3"]))
    assert "У-3" in msg(proj, "А-7") and "гипотеза" in msg(proj, "А-7")


def test_a8_item_without_claim_number(proj):
    edit_text(proj, "outputs/situation.md", "в месяц (У-2, факт).", "в месяц.")
    text = only_bad(proj, "А-8")
    assert "пункт 1" in text and "outputs/situation.md" in text


def test_a8_unknown_claim_and_latin_number(proj):
    edit_text(proj, "outputs/situation.md", "(У-4, прогноз)", "(У-99, прогноз)")
    assert "У-99 нет в claims.yaml" in msg(proj, "А-8")
    edit_text(proj, "outputs/situation.md", "(У-99, прогноз)", "(Y-4, прогноз)")
    assert "латинскими" in msg(proj, "А-8")


def test_a8_missing_section(proj):
    edit_text(proj, "outputs/situation.md", "## Известно", "## Что мы знаем")
    assert "нет раздела «Известно»" in only_bad(proj, "А-8")


def test_a8_missing_document_names_file_and_step(proj):
    (proj / "outputs" / "situation.md").unlink()
    text = msg(proj, "А-8")
    assert "нет файла outputs/situation.md" in text and "шаг 3" in text


def _dispute_text(proj):
    edit_text(
        proj,
        "outputs/situation.md",
        "## Где источники спорят\nНет.",
        "## Где источники спорят\n- У-2 и У-4: разные периоды.",
    )


def test_a9_pair_must_be_recorded(proj):
    _dispute_text(proj)
    text = only_bad(proj, "А-9")
    assert "У-2" in text and "У-4" in text and "dispute" in text


def test_a9_recorded_pair_passes(proj):
    _dispute_text(proj)

    def link(d):
        for c in d["claims"]:
            if c["id"] == "У-4":
                c["dispute"] = {"claim": "У-2", "reason": "разные периоды"}

    edit_yaml(proj, "claims.yaml", link)
    assert run(proj)["А-9"].ok


def test_a9_gap_with_disputing_sources_must_be_named(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["gaps"][1].update(reason="источники спорят"))
    text = only_bad(proj, "А-9")
    assert "П-2" in text and "источники спорят" in text


def test_a9_gap_named_with_pair_passes(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["gaps"][1].update(reason="источники спорят"))
    edit_text(
        proj,
        "outputs/situation.md",
        "## Где источники спорят\nНет.",
        "## Где источники спорят\n- П-2: У-2 и У-4 расходятся по периоду.",
    )

    def link(d):
        for c in d["claims"]:
            if c["id"] == "У-4":
                c["dispute"] = {"claim": "У-2", "reason": "период"}

    edit_yaml(proj, "claims.yaml", link)
    assert run(proj)["А-9"].ok


def test_a10_message_needs_a_fact(proj):
    edit_text(proj, "outputs/comms-strategy.md", "подтверждение У-2 (факт)", "подтверждение У-3 (гипотеза)")
    text = only_bad(proj, "А-10")
    assert "сообщение 1" in text and "«факт»" in text


def test_a10_channel_without_role(proj):
    edit_text(
        proj,
        "outputs/comms-strategy.md",
        "- Рассылка семьям — объясняет ценность в первую неделю (У-3).",
        "- Рассылка семьям",
    )
    text = only_bad(proj, "А-10")
    assert "канал 2" in text and "роль" in text


def test_a10_channels_section_missing(proj):
    edit_text(proj, "outputs/comms-strategy.md", "## Роли каналов", "## Каналы")
    assert "нет раздела «Роли каналов»" in msg(proj, "А-10")


def test_a11_two_searches_with_different_queries(proj):
    def same(d):
        d["gaps"][1]["searched"][1]["query"] = d["gaps"][1]["searched"][0]["query"].upper()

    edit_yaml(proj, "gaps.yaml", same)
    text = only_bad(proj, "А-11")
    assert "П-2" in text and "двух" in text


def test_a11_gap_closed_through_manager_needs_two_searches(proj):
    def one(d):
        d["gaps"][0].update(affects_choice=False, close_via="материал руководителя")
        d["gaps"][0]["searched"].pop()

    edit_yaml(proj, "gaps.yaml", one)
    edit_yaml(proj, "options.yaml", lambda d: None)
    assert "П-1" in msg(proj, "А-11")


def test_a12_open_gap_without_options(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["gaps"][0].update(affects_options=[]))
    text = msg(proj, "А-12")
    assert "П-1" in text and "affects_options" in text


def test_a12_unknown_option(proj):
    edit_yaml(proj, "gaps.yaml", lambda d: d["gaps"][0].update(affects_options=["Вар-9"]))
    assert "Вар-9 нет в options.yaml" in msg(proj, "А-12")


def test_a13_chosen_empty_and_unknown(proj):
    edit_yaml(proj, "options.yaml", lambda d: d.pop("chosen"))
    assert "chosen: пусто" in only_bad(proj, "А-13")
    edit_yaml(proj, "options.yaml", lambda d: d.update(chosen="Вар-9"))
    assert "Вар-9" in msg(proj, "А-13")


def _hypothesis(claim_id: str = "У-7") -> dict:
    return {
        "id": claim_id,
        "text": "Семьи не станут читать рассылку",
        "type": "гипотеза",
        "origin": "агент",
        "step": "5",
        "attempt": 1,
        "subject": "чтение рассылки",
        "basis": ["У-2"],
        "check_method": "доля открытий рассылки за первую неделю",
        "status": "действует",
        "status_history": [{"status": "действует", "at": "2026-11-06T10:41:00+03:00", "by": "агент"}],
    }


def test_a14_hypothesis_without_row(proj):
    edit_yaml(proj, "claims.yaml", lambda d: d["claims"].append(_hypothesis()))
    text = only_bad(proj, "А-14")
    assert "У-7" in text and "rows[].hypothesis" in text


def test_a14_exception_for_basis_of_foreign_objection_only(proj):
    edit_yaml(proj, "claims.yaml", lambda d: d["claims"].append(_hypothesis()))
    edit_yaml(proj, "options.yaml", lambda d: d["options"][0]["objections"][0].update(basis="У-7"))
    assert run(proj)["А-14"].ok  # Вар-1 не выбран


def test_a14_no_exception_for_chosen_option_or_other_mention(proj):
    edit_yaml(proj, "claims.yaml", lambda d: d["claims"].append(_hypothesis()))
    edit_yaml(proj, "options.yaml", lambda d: d["options"][1]["objections"][0].update(basis="У-7"))
    assert "У-7" in msg(proj, "А-14")  # Вар-2 выбран
    edit_yaml(proj, "options.yaml", lambda d: d["options"][1]["objections"][0].update(basis="гипотеза"))
    edit_yaml(proj, "options.yaml", lambda d: d["options"][0]["objections"][0].update(basis="У-7"))
    edit_yaml(proj, "options.yaml", lambda d: d["options"][0].update(main_assumption="Верно У-7 или нет"))
    assert "У-7" in msg(proj, "А-14")  # упоминание не только как basis


def test_a14_exception_needs_chosen(proj):
    edit_yaml(proj, "claims.yaml", lambda d: d["claims"].append(_hypothesis()))
    edit_yaml(proj, "options.yaml", lambda d: d["options"][0]["objections"][0].update(basis="У-7"))
    edit_yaml(proj, "options.yaml", lambda d: d.pop("chosen"))
    assert "У-7" in msg(proj, "А-14")


def test_a15_threshold_differs(proj):
    def other(d):
        for r in d["rows"]:
            if r.get("from_change_mind") == 1:
                r["change_mind"]["threshold"] = "ниже 25 %"

    edit_yaml(proj, "verification.yaml", other)
    text = only_bad(proj, "А-15")
    assert "ПП-5" in text and "не совпадает" in text


def test_a15_threshold_ignores_case_and_spaces(proj):
    def same(d):
        for r in d["rows"]:
            if r.get("from_change_mind") == 1:
                r["change_mind"]["threshold"] = "Ниже  20%"

    edit_yaml(proj, "verification.yaml", same)
    assert run(proj)["А-15"].ok


def test_a15_no_row_for_change_mind(proj):
    def drop(d):
        for r in d["rows"]:
            r.pop("from_change_mind", None)

    edit_yaml(proj, "verification.yaml", drop)
    assert "from_change_mind: 1" in only_bad(proj, "А-15")


def test_a16_premortem_row_must_be_defensive(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["premortem"][0].update(row="ПП-1"))
    text = only_bad(proj, "А-16")
    assert "ПП-1" in text and "защитный" in text


def test_a16_premortem_claim_must_be_hypothesis(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["premortem"][0].update(claim="У-2"))
    assert "У-2" in msg(proj, "А-16") and "не гипотеза" in msg(proj, "А-16")


def test_a17_no_undesired_effects(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d.update(undesired_effects=[]))
    assert "undesired_effects: пусто" in only_bad(proj, "А-17")


def test_a18_empty_and_missing_sections(proj):
    edit_text(proj, "outputs/comms-strategy.md", "## Барьер\n", "## Барьер\n\n")
    edit_text(
        proj,
        "outputs/comms-strategy.md",
        "Семья не понимает, зачем платить отдельно (непонимание); "
        "барьер снимает сообщение о цене и пробном периоде.\n",
        "",
    )
    assert "раздел «Барьер» пуст" in only_bad(proj, "А-18")
    edit_text(proj, "outputs/product-strategy.md", "## Осознанные отказы", "## Отказы")
    assert "нет раздела «Осознанные отказы»" in msg(proj, "А-18")


def test_a18_template_placeholder_is_empty():
    assert is_empty_body("<3–5 строк: для кого>") and is_empty_body("\n \n")
    assert not is_empty_body("Нет.")


def test_a19_no_labels_and_unknown_label(proj):
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]", "[О-9]")
    assert "[О-9]" in msg(proj, "А-19") and "не ведёт" in msg(proj, "А-19")
    text = (
        (proj / "outputs/comms-strategy.md")
        .read_text(encoding="utf-8")
        .replace("[О-9]", "")
        .replace("[О-1]", "")
    )
    (proj / "outputs/comms-strategy.md").write_text(text, encoding="utf-8")
    assert "нет ни одной метки" in msg(proj, "А-19")


def test_a19_latin_o_in_label(proj):
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]", "[O-1]")
    assert "латинской" in msg(proj, "А-19")


def test_a20_unconfirmed_promise_in_one_pager_and_comms(proj):
    edit_text(proj, "outputs/one-pager.md", "Сообщение о цене [О-1].", "Защита блокирует всё [О-2].")
    text = only_bad(proj, "А-20")
    assert "one-pager.md" in text and "О-2" in text
    edit_text(proj, "outputs/comms-strategy.md", "[О-1]; обещание", "[О-2]; обещание")
    assert "comms-strategy.md" in msg(proj, "А-20")


def test_a20_belongs_to_step_7(proj):
    edit_text(proj, "outputs/one-pager.md", "Сообщение о цене [О-1].", "Защита блокирует всё [О-2].")
    assert "А-20" not in run(proj, "6")


def test_a21_unknown_number_and_latin_letters(proj):
    edit_text(proj, "outputs/product-strategy.md", "(Вар-3)", "(Вар-9)")
    assert "Вар-9" in only_bad(proj, "А-21")
    edit_text(proj, "outputs/product-strategy.md", "(Вар-9)", "(Bap-3)")
    text = msg(proj, "А-21")
    assert "Bap-3" in text and "Вар-3" in text and "латинск" in text


def test_a21_checks_every_prefix(proj):
    for token in ("И-9", "У-9", "П-9", "ВП-9", "ПП-9"):
        edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", f"## Диагноз\nСм. {token}.\n")
        assert f"{token} нет в" in msg(proj, "А-21"), token
        edit_text(proj, "outputs/product-strategy.md", f"См. {token}.\n", "")


def test_a21_promise_number_only_in_audience_documents(proj):
    edit_text(proj, "outputs/product-strategy.md", "## Диагноз\n", "## Диагноз\nСм. О-9.\n")
    assert run(proj)["А-21"].ok
    edit_text(proj, "outputs/one-pager.md", "## Кратко\n", "## Кратко\nСм. О-9.\n")
    assert "О-9 нет в promises.yaml" in msg(proj, "А-21")


def test_a21_ignores_words_that_only_look_like_numbers(proj):
    edit_text(
        proj,
        "outputs/product-strategy.md",
        "## Диагноз\n",
        "## Диагноз\nТариф ИИ-защита, Wi-Fi 6, 2026-11-06, ВПН-1.\n",
    )
    assert run(proj)["А-21"].ok


def _ground_free(proj, word_in_docs: str = "абонентов"):
    """Проект, в котором ни запрос, ни рамка, ни снимки, ни материалы не называют абонентов."""
    for rel in ("inputs/report.txt", "sources/i-1.txt"):
        path = proj / rel
        path.write_text(path.read_text(encoding="utf-8").replace(word_in_docs, "клиентов"), encoding="utf-8")


def test_a22_word_of_the_default_area_without_ground(proj):
    _ground_free(proj)
    text = only_bad(proj, "А-22")
    assert "«абонент»" in text and "outputs/situation.md" in text


def test_a22_same_word_in_the_frame_passes(proj):
    _ground_free(proj)
    edit_yaml(
        proj, "frame.yaml", lambda d: d["constraints"].update(value="только открытые источники об абонентах")
    )
    assert run(proj)["А-22"].ok


def test_a22_word_in_snapshot_or_material_passes(proj):
    _ground_free(proj)
    edit_yaml(proj, "frame.yaml", lambda d: d["constraints"].update(value="только открытые источники"))
    (proj / "sources" / "i-1.txt").write_text("Условия для абонента: 199 рублей.", encoding="utf-8")
    assert run(proj)["А-22"].ok


def test_a22_compares_lemmas_and_phrases(proj):
    _ground_free(proj)
    edit_yaml(
        proj, "frame.yaml", lambda d: d["constraints"].update(value="только открытые источники об абонентах")
    )
    edit_text(
        proj,
        "outputs/one-pager.md",
        "## Кратко\n",
        "## Кратко\nУлучшим покрытие рядом с базовыми станциями.\n",
    )
    assert "«базовая станция»" in msg(proj, "А-22")
    (proj / "inputs" / "report.txt").write_text("Базовая станция оператора связи.", encoding="utf-8")
    assert run(proj)["А-22"].ok
    # слова сочетания по отдельности не дают сочетания
    (proj / "inputs" / "report.txt").write_text("Станция метро, базовый курс.", encoding="utf-8")
    assert "«базовая станция»" in msg(proj, "А-22")


def test_a22_new_task_outside_telecom(tmp_path):
    """Задача не из телекома: «абонент» в документах без основания — «нет»; в рамке — «да»."""
    root = tmp_path / "p"
    shutil.copytree(COMPLETE, root)
    for rel in ("inputs/report.txt", "sources/i-1.txt"):
        (root / rel).write_text("Курс программирования для школьников. Цена 5000 рублей.", encoding="utf-8")
    edit_yaml(root, "frame.yaml", lambda d: d["decision"].update(value="Как продавать курс школьникам"))
    assert not run(root)["А-22"].ok
    edit_yaml(root, "frame.yaml", lambda d: d["constraints"].update(value="абонент платит раз в месяц"))
    assert run(root)["А-22"].ok


def test_a23_counts_project_errors(proj):
    edit_yaml(proj, "options.yaml", lambda d: d.update(chosen="Вар-77"))
    text = msg(proj, "А-23")
    assert "ошибок: 1" in text and "chosen" in text


def test_a24_hits_choice_needs_a_stop_after_b4(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["premortem"][0].update(hits_choice=True))
    text = only_bad(proj, "А-24")
    assert "ПП-2" in text and "У-5" in text and "внеплановой остановки" in text


def _stop(message: str, time: str = "10:48:00") -> dict:
    return {
        "schema_version": 1,
        "time": f"2026-11-06T{time}+03:00",
        "actor": "CLI",
        "kind": "действие",
        "step": "5",
        "name": "внеплановая остановка",
        "idempotency_hash": "a" * 64,
        "status": "выполнено",
        "duration_s": 0,
        "message": message,
    }


def _insert_stop(proj, stop: dict) -> None:
    def put(events):
        at = next(i for i, e in enumerate(events) if e["time"] > stop["time"])
        events.insert(at, stop)

    edit_journal(proj, put)


def test_a24_stop_with_the_row_or_the_claim_passes(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["premortem"][0].update(hits_choice=True))
    _insert_stop(proj, _stop("причина пре-мортема ПП-2 бьёт по выбору"))
    assert run(proj)["А-24"].ok


def test_a24_stop_before_b4_or_with_other_number_does_not_count(proj):
    edit_yaml(proj, "verification.yaml", lambda d: d["premortem"][0].update(hits_choice=True))
    _insert_stop(proj, _stop("причина ПП-20 бьёт по выбору"))
    assert not run(proj)["А-24"].ok
    _insert_stop(proj, _stop("причина ПП-2 бьёт по выбору", "10:26:00"))  # до ворот В4
    assert not run(proj)["А-24"].ok


def test_a25_recommendation_on_company_claim_only(proj):
    edit_yaml(proj, "options.yaml", lambda d: d["recommendation"].update(claims=["У-4"]))
    assert "self_reported: false" in only_bad(proj, "А-25")


# --- устойчивость ----------------------------------------------------------


def test_checks_do_not_fall_on_an_incomplete_project(tmp_path, cli):
    assert cli("new", "bare")[0] == 0
    root = tmp_path / "projects" / "bare"
    for step in ("1", "6", "7"):
        results = run(root, step)
        assert results
        assert not any("проверка не выполнена" in p for r in results.values() for p in r.problems)
    assert not run(root, "6")["А-1"].ok
    assert "шаг 1" in " ".join(run(root, "6")["А-1"].problems)


def test_checks_survive_garbage_files(proj):
    for rel in ("frame.yaml", "plan.yaml", "gaps.yaml", "options.yaml", "promises.yaml", "verification.yaml"):
        (proj / rel).write_bytes(b"\xff\xfe\x00 not yaml")
    (proj / "outputs" / "situation.md").write_bytes(b"\xff\xfe\x00")
    (proj / "journal.jsonl").write_text("не json\n", encoding="utf-8")
    results = run(proj)
    assert len(results) == 25
    assert not any("проверка не выполнена" in p for r in results.values() for p in r.problems)


# --- CLI -------------------------------------------------------------------


def test_cli_check_exit_codes_and_json(cli):
    code, out, err = cli("rubric", "check", "--project", str(COMPLETE))
    assert code == 0, err
    assert "Итого: да — 25, нет — 0" in out and "А-1  required_files_present  да" in out
    code, out, _ = cli("rubric", "check", "--project", str(COMPLETE), "--json", "--step", "6")
    data = json.loads(out)
    assert (
        code == 0
        and data["ok"]
        and data["step"] == "6"
        and len(data["checks"]) == 24
        and data["failed"] == []
    )


def test_cli_check_reports_no_and_writes_nothing(cli, tmp_path):
    root = tmp_path / "p"
    shutil.copytree(COMPLETE, root)
    (root / "plan.yaml").unlink()
    before = tree_hashes(root)
    code, out, _ = cli("rubric", "check", "--project", str(root))
    assert code == 1
    assert "А-5  plan_complete  нет" in out and "нет файла plan.yaml" in out
    code, out, _ = cli("rubric", "check", "--project", str(root), "--json")
    data = json.loads(out)
    assert code == 1 and not data["ok"] and "А-5" in data["failed"]
    failed = next(c for c in data["checks"] if c["id"] == "А-5")
    assert failed["answer"] == "нет" and failed["problems"] and failed["check"] == "plan_complete"
    assert tree_hashes(root) == before
    assert len(journal_of(root)) == len(journal_of(COMPLETE))


def test_cli_check_takes_current_step_from_state(cli, tmp_path):
    root = tmp_path / "p"
    shutil.copytree(COMPLETE, root)
    edit_yaml(root, "state.yaml", lambda d: d.update(step="7"))
    code, out, _ = cli("rubric", "check", "--project", str(root), "--json")
    assert json.loads(out)["step"] == "7" and len(json.loads(out)["checks"]) == 25
    code, out, err = cli("rubric", "check", "--project", str(root), "--step", "9")
    assert code == 2 and "9" in err


# --- разбор Markdown ---------------------------------------------------------


def test_sections_ignore_code_fences_and_match_title_with_tail():
    text = "# Док\n\n## Кратко\nа\n\n```\n## Не раздел\n```\n\n## Диагноз: что мешает\nб\n"
    assert set(sections(text)) == {"Кратко", "Диагноз: что мешает"}
    assert find_section(text, "Диагноз") == "б"
    assert find_section(text, "Диаг") is None


def test_list_items_cover_bullets_numbers_tables_and_continuations():
    body = (
        "- первый\n  продолжение У-1\n  - вложенный У-2\n2. второй\nлениво\n\nабзац\n\n"
        "| a | b |\n|---|---|\n| У-3 | x |\n"
    )
    items = list_items(body)
    assert len(items) == 3
    assert "У-1" in items[0] and "У-2" in items[0] and "лениво" in items[1]
    assert items[2].startswith("| У-3")


def test_refs_need_russian_letters_and_whole_numbers():
    text = "У-3, У-30, ВП-3, [У-3], Y-3, у-3, ПП-3"
    assert refs(text, "У") == ["У-3", "У-30"]
    assert refs(text, "П") == []
    assert refs(text, "ПП") == ["ПП-3"] and refs(text, "ВП") == ["ВП-3"]
