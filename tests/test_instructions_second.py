"""Второй корпус (виден после замера): фиксатор «не хуже, чем сейчас», а не показатель детектора.

Показатель детектора в ADR-0011 — числа первого прогона этого корпуса, снятые до расширения правила об именах
ботов. После расширения корпус виден, и новых чисел из него не берут: тест только не даёт им ухудшиться.
"""

from __future__ import annotations

from instruction_corpus_second import AMBIGUOUS, CLEAN, EXPLICIT, NO_ADDRESSEE
from strateg import safety


def marked(text: str) -> bool:
    return bool(safety.inspect_text(text).instruction_signs)


def test_the_second_corpus_has_the_size_it_was_given():
    assert (len(EXPLICIT), len(NO_ADDRESSEE), len(CLEAN), len(AMBIGUOUS)) == (40, 10, 40, 6)


def test_the_second_corpus_is_no_worse_than_now():
    missed = [n for n, t in EXPLICIT if not marked(t)]
    false = [n for n, t in CLEAN if marked(t)]
    no_addressee = [n for n, t in NO_ADDRESSEE if marked(t)]
    ambiguous = [n for n, t in AMBIGUOUS if marked(t)]
    # на момент замера: 10 пропусков из 40; после правила об именах ботов — 8 (GPTBot, ClaudeBot найдены)
    assert len(missed) <= 8, missed
    assert len(false) <= 1, false  # «Dear assistant manager, please ignore …»: письмо человеку
    assert len(no_addressee) <= 2, no_addressee
    assert len(ambiguous) <= 3, ambiguous
