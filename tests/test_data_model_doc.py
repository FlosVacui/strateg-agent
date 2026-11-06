"""Описание модели данных: коротко и говорит то, что делает код."""

from __future__ import annotations

import typing
from pathlib import Path

from strateg import models

DOC = (Path(__file__).resolve().parent.parent / "docs" / "data-model.md").read_text(encoding="utf-8")


def test_doc_fits_two_screens():
    assert len(DOC.splitlines()) <= 70
    assert len(DOC.encode("utf-8")) <= 11000


def test_doc_says_what_is_left_for_the_next_package():
    assert "сверка чисел и отрицаний" in DOC
    assert "пересчёт нечёткого сходства" in DOC
    assert "пакет 3.2" in DOC
    assert "будет 09.11" in DOC


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
