from pathlib import Path

import check_links


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def problems(root):
    return check_links.find_problems(root)[1]


def test_ok_and_broken_file(tmp_path):
    write(tmp_path, "a.md", "# А\n[да](b.md) [нет](c.md)\n")
    write(tmp_path, "b.md", "# Б\n")
    found = problems(tmp_path)
    assert len(found) == 1 and "нет файла c.md" in found[0]


def test_percent_encoding_in_path_and_anchor(tmp_path):
    anchor = "%D0%B7%D0%B0%D0%B3%D0%BE%D0%BB%D0%BE%D0%B2%D0%BE%D0%BA"  # «заголовок»
    write(tmp_path, "a.md", f"[x](my%20doc.md) [y](my%20doc.md#{anchor})\n")
    write(tmp_path, "my doc.md", "# Заголовок\n")
    assert problems(tmp_path) == []


def test_duplicate_headings_get_github_suffixes(tmp_path):
    write(tmp_path, "a.md", "[1](b.md#итог) [2](b.md#итог-1) [3](b.md#итог-2) [4](b.md#итог-3)\n")
    write(tmp_path, "b.md", "# Итог\n## Итог\n### Итог\n")
    found = problems(tmp_path)
    assert len(found) == 1 and "#итог-3" in found[0]


def test_missing_anchor(tmp_path):
    write(tmp_path, "a.md", "[x](b.md#нет)\n")
    write(tmp_path, "b.md", "# Есть\n")
    assert "нет якоря #нет" in problems(tmp_path)[0]


def test_personal_projects_skipped_but_example_and_demo_checked(tmp_path):
    write(tmp_path, "projects/README.md", "# Проекты\n")
    write(tmp_path, "projects/mine/x.md", "[битая](nowhere.md)\n")
    write(tmp_path, "projects/demo-client/x.md", "[битая](nowhere.md)\n")
    assert problems(tmp_path) == []
    write(tmp_path, "projects/example/x.md", "[битая](nowhere.md)\n")
    write(tmp_path, "projects/demo/x.md", "[битая](nowhere.md)\n")
    found = problems(tmp_path)
    assert len(found) == 2
    assert all(f.startswith(("projects/example/", "projects/demo/")) for f in found)


def test_external_links_code_and_images_ignored(tmp_path):
    write(tmp_path, "a.md", "[w](https://x.y/z) `[c](no.md)`\n```\n[f](no.md)\n```\n![i](no.png)\n")
    assert problems(tmp_path) == []


def test_docstring_has_real_script_path():
    source = Path(check_links.__file__).read_text(encoding="utf-8")
    assert "python scripts/check_links.py" in source
