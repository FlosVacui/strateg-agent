"""Список слов предметной области для А-22: общий для области, без слов учебного кейса."""

from __future__ import annotations

import re

import yaml

from strateg.rubric_checks import knowledge_dir, load_domain_words, phrase_lemmas
from test_skills import CASE_PHRASES, CASE_WORDS
from test_skills import CASE_STRINGS as SKILL_CASE_STRINGS

PATH = knowledge_dir() / "rubrics" / "domain-words.yaml"
# Строки кейса из списка копии для прогона. «Абонент» и «тариф» в строках скиллов — как раз слова области
# по умолчанию, ради которых список существует; в список они входят.
RUN_COPY_NEEDLES = ("Защита близких", "Семейная безопасность", "fraud-protection", "expected.md", "мошенн")
DOMAIN = {"абонент", "тариф"}


def test_list_has_30_to_60_words():
    words = load_domain_words()
    assert 30 <= len(words) <= 60
    assert len(set(w.casefold() for w in words)) == len(words)


def test_default_words_of_the_area_are_there():
    words = {w.casefold() for w in load_domain_words()}
    for w in (
        "абонент",
        "тариф",
        "оператор связи",
        "sim-карта",
        "роуминг",
        "базовая станция",
        "мобильный интернет",
    ):
        assert w in words, w


def test_no_strings_of_the_study_case():
    text = PATH.read_text(encoding="utf-8")
    low = text.casefold()
    needles = [
        *RUN_COPY_NEEDLES,
        *(s for s in SKILL_CASE_STRINGS if s.casefold() not in DOMAIN),
        *CASE_PHRASES,
    ]
    assert [n for n in needles if n.casefold() in low] == []
    for w in (*CASE_WORDS, "Ева"):
        assert re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) is None


def test_every_entry_is_made_of_words_and_has_lemmas():
    data = yaml.safe_load(PATH.read_text(encoding="utf-8"))
    assert set(data) == {"words"}
    for w in data["words"]:
        assert isinstance(w, str) and phrase_lemmas(w), w
