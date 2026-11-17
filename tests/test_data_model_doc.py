"""Описание модели данных: коротко и говорит то, что делает код."""

from __future__ import annotations

import typing
from pathlib import Path

from strateg import models

DOC = (Path(__file__).resolve().parent.parent / "docs" / "data-model.md").read_text(encoding="utf-8")


def test_doc_fits_two_screens():
    assert len(DOC.splitlines()) <= 100
    assert len(DOC.encode("utf-8")) <= 16500


def test_doc_says_what_is_left_for_the_next_package():
    assert "сверка чисел и отрицаний" in DOC
    assert "нечётк" not in DOC  # нечёткого пути проверки цитаты больше нет
    assert "команды CLI" in DOC
    assert "09.11" not in DOC


def test_doc_lists_every_access_value_and_action_status():
    for literal in (models.Access, models.ActionStatus):
        for value in typing.get_args(literal):
            assert value in DOC, value
    assert "источник недоступен: ошибка" in DOC and "С-9" in DOC
    assert "прервано" in DOC


def test_doc_does_not_mention_removed_helpers():
    for name in ("work_minutes", "total_cost_usd", "changed_outside_agent"):
        assert name not in DOC


def test_doc_mentions_step_and_attempt():
    assert "`step`" in DOC and "`attempt`" in DOC


def test_doc_lists_every_file_of_the_project_and_its_schema():
    for name in ("frame", "plan", "gaps", "options", "promises", "verification", "decisions", "metrics"):
        assert f"`{name}.yaml`" in DOC and f"`{name}.schema.json`" in DOC, name
    assert "history/frame.v1.yaml" in DOC and "history/outputs/options.v2.md" in DOC
    assert "history/release-1/" in DOC


def test_doc_describes_the_rules_the_main_part_lacked():
    assert "нельзя цитировать без подтверждения" in DOC
    for field in ("close_via", "hypothesis", "from_change_mind", "metric_coverage", "gate.frame"):
        assert f"`{field}`" in DOC, field
    for number in ("ВП-n", "П-n", "Вар-n", "О-n", "ПП-n", "Реш-n"):
        assert number in DOC, number


def test_doc_keeps_the_rule_that_an_incompatible_change_goes_to_the_changelog():
    rule = next(line for line in DOC.splitlines() if "schema_version" in line and "Несовместимое" in line)
    assert "поднимает `schema_version`" in rule and "`CHANGELOG.md`" in rule


def test_doc_keeps_the_note_on_output_files():
    assert "Семь файлов выводов необязательны" in DOC and "поля для проверок — в YAML" in DOC
