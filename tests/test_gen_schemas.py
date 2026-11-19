"""JSON Schema в `schemas/` совпадает с моделями и генерируется детерминированно."""

from __future__ import annotations

import json
import re
from pathlib import Path

import gen_schemas
from strateg import models

ROOT = Path(__file__).resolve().parent.parent


def test_committed_schemas_match_models():
    assert gen_schemas.find_problems(ROOT / "schemas") == []


def test_one_schema_per_file_of_the_project():
    names = {p.name for p in (ROOT / "schemas").glob("*.schema.json")}
    assert names == {
        "state.schema.json",
        "sources.schema.json",
        "claims.schema.json",
        "journal-event.schema.json",
        "cost-record.schema.json",
        "frame.schema.json",
        "plan.schema.json",
        "gaps.schema.json",
        "options.schema.json",
        "promises.schema.json",
        "verification.schema.json",
        "decisions.schema.json",
        "metrics.schema.json",
        "completeness.schema.json",
        "calc-result.schema.json",
    }


def test_output_is_deterministic():
    assert gen_schemas.expected() == gen_schemas.expected()


def test_output_is_utf8_lf_without_escapes():
    for path in (ROOT / "schemas").glob("*.schema.json"):
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and data.endswith(b"\n")
        assert b"\\u04" not in data  # кириллица записана символами, а не \uXXXX
        json.loads(data.decode("utf-8"))


def test_every_schema_requires_schema_version_and_forbids_extra_fields():
    for path in (ROOT / "schemas").glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert "schema_version" in schema["required"]
        assert schema["properties"]["schema_version"]["const"] == 1
        assert schema["additionalProperties"] is False


def test_check_detects_drift_and_stale_files(tmp_path):
    gen_schemas.write(tmp_path)
    assert gen_schemas.find_problems(tmp_path) == []
    (tmp_path / "state.schema.json").write_bytes(b"{}\n")
    (tmp_path / "old.schema.json").write_bytes(b"{}\n")
    (tmp_path / "claims.schema.json").unlink()
    problems = gen_schemas.find_problems(tmp_path)
    assert any("state.schema.json не совпадает" in p for p in problems)
    assert any("лишний файл old.schema.json" in p for p in problems)
    assert any("нет файла схемы claims.schema.json" in p for p in problems)


def test_path_pattern_in_schema_agrees_with_validator():
    """Шаблон пути из схемы и проверка в коде дают один ответ."""
    pattern = re.compile(models._PATH_PATTERN)
    schema_text = (ROOT / "schemas" / "sources.schema.json").read_text(encoding="utf-8")
    assert json.dumps(models._PATH_PATTERN, ensure_ascii=False) in schema_text
    good = ["inputs/a.pdf", "sources/i-1.txt", "inputs/папка/файл.txt", "a..b/c", "inputs/.hidden"]
    good += ["a b/c d.txt", "inputs/файл (1).txt", "x"]
    bad = ["/etc/a", "C:\\x", "d:/x", "\\\\srv\\s", "inputs\\a", "../a", "a/../b", "a/..", ".."]
    bad += ["inputs//a.pdf", "inputs/./a.pdf", "./a", "inputs/a.pdf ", "inputs/a.pdf/", "inputs/a.pdf."]
    bad += [" inputs/a.pdf", "inputs/ a.pdf", "inputs/a\x00b", ".", "a/.", "inputs/"]
    for value in good:
        assert pattern.match(value), value
        assert models._check_rel_path(value) == value
    for value in bad:
        assert not pattern.match(value), value
        try:
            models._check_rel_path(value)
        except ValueError:
            continue
        raise AssertionError(value)
