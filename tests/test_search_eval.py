"""Замер ADR-0012 (`scripts/eval_search.py`): пороги на долю найденного, контекст и время повтора."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

import search_corpus
from strateg import search

ROOT = Path(__file__).resolve().parent.parent

# Фактическая доля при замере — 20 из 20; порог ниже на запас, чтобы мелкая правка текстов не роняла тест.
FORMS_MIN_FOUND = 18
CONTEXT_MAX_SHARE = 0.10


def load_script():
    spec = importlib.util.spec_from_file_location("eval_search", ROOT / "scripts" / "eval_search.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """Замер по вымышленному проекту: три способа поиска, 20 запросов."""
    import os

    folder = tmp_path_factory.mktemp("eval") / "projects"
    old = os.environ.get("STRATEG_PROJECTS_DIR")
    os.environ["STRATEG_PROJECTS_DIR"] = str(folder)
    try:
        search_corpus.build_project("corpus")
        yield load_script().evaluate(folder / "corpus")
    finally:
        if old is None:
            os.environ.pop("STRATEG_PROJECTS_DIR", None)
        else:
            os.environ["STRATEG_PROJECTS_DIR"] = old


def way(measured, name: str) -> dict:
    return next(w for w in measured["ways"] if w["way"].startswith(name))


def test_there_are_twenty_queries_and_enough_of_each_kind(measured):
    assert measured["queries"] == 20 == len(search_corpus.QUERIES)
    notes = [q[3] for q in search_corpus.QUERIES]
    assert sum(1 for n in notes if n != "как в тексте") >= 10  # не меньше половины — другой падеж или число
    assert sum(1 for n in notes if "беглая" in n) >= 2 and sum(1 for n in notes if "синоним" in n) >= 2
    assert sum(1 for q in search_corpus.QUERIES if q[1]) >= 2  # синонимы через `also`


def test_trigram_search_with_word_forms_finds_the_answer_in_the_first_five(measured):
    forms = way(measured, "триграммы, формы слова")
    assert forms["of"] == 20 and forms["found"] >= FORMS_MIN_FOUND
    assert forms["top3"] >= FORMS_MIN_FOUND - 2


def test_forms_beat_a_search_without_forms_and_a_plain_substring(measured):
    forms = way(measured, "триграммы, формы слова")
    for other in ("триграммы, без приведения слов", "unicode61, без приведения слов", "подстрока по файлам"):
        assert forms["found"] > way(measured, other)["found"], other
        assert forms["mrr"] > way(measured, other)["mrr"], other


def test_unicode61_with_forms_is_a_working_fallback(measured):
    fallback = way(measured, "unicode61, формы слова")
    assert fallback["found"] >= FORMS_MIN_FOUND


def test_stem_rule_is_not_better_than_word_forms(measured):
    forms, stem = way(measured, "триграммы, формы слова"), way(measured, "триграммы, основа по правилу")
    assert forms["found"] >= stem["found"] and forms["together"] >= stem["together"]


def test_search_output_is_a_small_part_of_the_files(measured):
    context = measured["context"]
    assert context["share_of_all"] <= CONTEXT_MAX_SHARE
    assert context["share_of_answer_files"] <= CONTEXT_MAX_SHARE * 2
    assert context["search_chars"] < 2000


def test_repeat_search_takes_under_a_second_without_python_start(home, clock):
    p = search_corpus.build_project("corpus")
    first_started = time.perf_counter()
    index = search.open_project_index(p.root)
    search.run_search(
        [index], ["нехватка курьеров"], frozenset(search.DEFAULT_AREAS), limit=8, only_active=False
    )
    index.close()
    first = time.perf_counter() - first_started
    started = time.perf_counter()
    index = search.open_project_index(p.root)
    assert not index.rebuilt
    search.run_search(
        [index], ["нехватка курьеров"], frozenset(search.DEFAULT_AREAS), limit=8, only_active=False
    )
    index.close()
    assert time.perf_counter() - started < 1.0
    assert first < 30  # построение индекса на 25 источниках и 60 утверждениях
