"""Подсекция «Таблицы и расчёты» в скилле research и документы про `data` и `calc` (ДВ-4, С-8)."""

from __future__ import annotations

import re
from pathlib import Path

from strateg import models

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / ".claude" / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
MAX_SECTION_BYTES = 1024


def section() -> str:
    m = re.search(r"^### Таблицы и расчёты\n(.*?)(?=^#{2,3} )", SKILL, re.S | re.M)
    assert m, "в скилле research нет подсекции «Таблицы и расчёты»"
    return m.group(0)


def test_subsection_is_short_and_says_the_four_rules():
    text = section()
    assert len(text.encode("utf-8")) <= MAX_SECTION_BYTES
    assert "uv run strateg data check" in text  # до выводов
    assert "не в уме" in text and "uv run strateg calc run" in text and "outputs/calc/" in text
    assert "calc: {name:" in text  # число в утверждении — со ссылкой на расчёт
    assert "в картину" in text and "ограничение" in text  # проблемы данных — в картину
    assert "uv run strateg calc check" in text


def test_subsection_sits_in_the_collection_step():
    assert SKILL.index("## Шаг 2б.") < SKILL.index("### Таблицы и расчёты") < SKILL.index("## Шаг 3.")


def test_skill_commands_exist_in_the_cli():
    from strateg.cli import build_parser

    parser = build_parser()
    for argv in (["data", "check", "И-1"], ["calc", "run", "x.py"], ["calc", "check"]):
        assert parser.parse_args(argv).handler


def test_documents_name_the_commands_and_the_limits():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    limits = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
    for word in ("strateg data check", "strateg calc run", "strateg calc check"):
        assert word in readme, word
    assert "Регрессий, сезонности" in limits
    assert "Нет: лимита памяти" in limits  # чего нет в изоляции, названо
    assert "лимита памяти" in readme  # и в README, коротко
    adr5 = (ROOT / "docs" / "adr" / "0005-claims-quote-check.md").read_text(encoding="utf-8")
    assert "`calc: {name, key}`" in adr5 and "а не локатор" in adr5
    model = (ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")
    assert (
        "`outputs/calc/<имя>.py`" in model
        and "calc-result.schema.json" in model
        and "`claims[].calc`" in model
    )
    assert "calc check" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_claim_has_the_calc_field_and_the_result_schema_exists():
    assert "calc" in models.Claim.model_fields
    assert "calc-result" in models.SCHEMA_MODELS
    assert (ROOT / "schemas" / "calc-result.schema.json").is_file()
