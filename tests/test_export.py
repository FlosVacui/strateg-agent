"""`strateg export html|pdf`: документы outputs/*.md в читаемом виде (ADR-0014, ОМ-5.1…ОМ-5.3, СД-2)."""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from helpers import tree_hashes
from strateg import export

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = ("project_complete", "project_follow_up")
DOCS = ("one-pager", "situation", "product-strategy", "comms-strategy")
HREF = re.compile(r'href="#([^"]+)"')
ID = re.compile(r'\bid="([^"]+)"')
REF = re.compile(r"(?<![\w-])([УИ])-([1-9][0-9]*)(?![\w-])")


@pytest.fixture
def work(tmp_path):
    """Копия фикстуры в папке с кириллицей и пробелом: фикстура не меняется."""

    def make(name: str = "project_complete") -> Path:
        root = tmp_path / "Мой проект" / name
        shutil.copytree(FIXTURES / name, root)
        return root

    return make


def registry(root: Path):
    return export._registry(root)


def check_links(html: str) -> None:
    ids = set(ID.findall(html))
    assert len(ids) == len(ID.findall(html)), "повторяющиеся id"
    broken = [h for h in HREF.findall(html) if h not in ids]
    assert not broken, f"ссылки без якоря: {broken}"


# --- HTML обеих фикстур ---------------------------------------------------------------------------


@pytest.mark.parametrize("project", PROJECTS)
@pytest.mark.parametrize("doc", DOCS)
def test_html_of_every_fixture_document(work, cli, project, doc):
    root = work(project)
    out = root.parent / "Готовое"
    code, text, err = cli("export", "html", doc, "--project", str(root), "--out", str(out))
    assert code == 0, err
    html = (out / f"{doc}.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and '<html lang="ru">' in html
    assert '<meta charset="utf-8">' in html
    source = (root / "outputs" / f"{doc}.md").read_text(encoding="utf-8")
    title = re.search(r"^# (.+)$", source, re.M).group(1)
    assert f"<title>{title}</title>" in html
    if "\n|" in source:
        assert "<table>" in html and "<th>" in html
    claims, sources = registry(root)
    code_free = re.sub(r"`[^`]*`", "", source)
    for kind, n in REF.findall(code_free):
        ref = f"{kind}-{n}"
        if ref in claims:
            assert f'href="#claim-{n}"' in html and f'id="claim-{n}"' in html
        elif ref in sources:
            assert f'href="#source-{n}"' in html and f'id="source-{n}"' in html
    for claim_id in re.findall(r'href="#claim-(\d+)"', html):
        claim = claims[f"У-{claim_id}"]
        for quote in claim.quotes:
            assert f'id="source-{quote.source.split("-")[1]}"' in html
    check_links(html)
    assert "Основания" in html
    assert "Подписка «Защита»" in html or "Кратко" in html  # кириллица цела


def test_appendix_has_only_cited_claims_and_their_sources(work):
    root = work()
    claims, sources = registry(root)
    r = export.render_document(
        "one-pager", (root / "outputs/one-pager.md").read_text("utf-8"), claims, sources
    )
    assert r.claims == ("У-2", "У-4")
    assert 'id="claim-2"' in r.html and 'id="claim-4"' in r.html
    assert 'id="claim-3"' not in r.html and 'id="claim-5"' not in r.html
    # источники — из цитат утверждений приложения: И-1 и И-2
    assert 'id="source-1"' in r.html and 'id="source-2"' in r.html and 'id="source-3"' not in r.html
    assert "https://example.com/protection/terms" in r.html and "06.11.2026" in r.html
    assert "inputs/report.pdf" in r.html
    assert "Стоимость подписки в Москве — 199 рублей в месяц" in r.html
    assert "RU-MOW; 2026" in r.html


def test_replaced_claim_shows_status_and_leads_to_replacement(work):
    root = work()
    claims, sources = registry(root)
    r = export.render_document("x", "Раньше считали так (У-1).", claims, sources)
    assert 'id="claim-1"' in r.html and "заменено" in r.html
    assert 'href="#claim-2"' in r.html and 'id="claim-2"' in r.html  # заменившее входит в приложение
    check_links(r.html)


def test_revoked_claim_shows_reason(work):
    root = work()
    claims, sources = registry(root)
    revoked = claims["У-3"].model_copy(
        update={
            "status": "отозвано",
            "status_history": [
                *claims["У-3"].status_history,
                claims["У-3"]
                .status_history[0]
                .model_copy(update={"status": "отозвано", "reason": "цитата не подтвердилась"}),
            ],
        }
    )
    r = export.render_document("x", "Гипотеза (У-3).", {**claims, "У-3": revoked}, sources)
    assert "отозвано" in r.html and "цитата не подтвердилась" in r.html


# --- безопасность ---------------------------------------------------------------------------------


def test_raw_html_and_script_from_markdown_are_escaped(work):
    root = work()
    claims, sources = registry(root)
    text = (
        "# Т\n\n<script>alert(1)</script>\n\n"
        'текст <b onclick="x()">жирный</b> <img src=x onerror=y>\n\n<div>блок</div>\n'
    )
    r = export.render_document("x", text, claims, sources)
    assert "<script" not in r.html and "<b " not in r.html and "<img" not in r.html and "<div>" not in r.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.html


def test_quote_and_yaml_fields_are_escaped(work):
    root = work()
    claims, sources = registry(root)
    claim = claims["У-2"]
    quote = claim.quotes[0].model_copy(update={"text": '<script>alert("q")</script> & «199»'})
    evil = claim.model_copy(
        update={"quotes": [quote], "text": '<img src=x onerror=alert(1)> "текст"', "region": "<i>RU</i>"}
    )
    src = sources["И-1"].model_copy(update={"url": 'https://e.example/a"onmouseover="x'})
    r = export.render_document("x", "(У-2)", {**claims, "У-2": evil}, {**sources, "И-1": src})
    assert "<script" not in r.html and "<img" not in r.html and "<i>" not in r.html
    assert "&lt;script&gt;" in r.html and "&amp; «199»" in r.html
    assert 'href="https://e.example/a&quot;onmouseover=&quot;x"' in r.html
    assert '" onmouseover' not in r.html


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:x",
        "file:///etc/passwd",
        "//evil.example/x",
        "data:image/png;base64,AAAA",
    ],
)
def test_dangerous_link_scheme_is_not_a_link(link):
    r = export.render_document("x", f"[нажми]({link}) и [ещё](https://example.com/ok)", {}, {})
    hrefs = re.findall(r'href="([^"]*)"', r.html)
    assert hrefs == ["https://example.com/ok"]  # единственная ссылка; опасная осталась текстом
    assert "<a " not in r.html.replace('<a href="https://example.com/ok">', "")
    assert '<a href="https://example.com/ok">ещё</a>' in r.html
    assert "нажми" in r.html


@pytest.mark.parametrize(
    "link", ["#claim-2", "mailto:a@example.com", "inputs/report.pdf", "../x/y.md", "HTTP://EXAMPLE.COM/a"]
)
def test_safe_links_stay_links(link):
    r = export.render_document("x", f"[см.]({link})", {}, {})
    assert re.findall(r'href="([^"]*)"', r.html) == [link]


def test_images_are_never_loaded_only_caption_stays():
    text = (
        "![схема цены](http://evil.example/x.png) и ![](data:image/png;base64,AAAA) "
        "и ![локальная](inputs/a.png)"
    )
    r = export.render_document("x", text, {}, {})
    assert "<img" not in r.html and "src=" not in r.html and "evil.example" not in r.html
    assert "[изображение: схема цены]" in r.html and "[изображение: локальная]" in r.html
    assert "[изображение]" in r.html


def test_no_external_resources_and_csp_in_every_fixture_html(work, cli):
    for project in PROJECTS:
        root = work(project)
        assert cli("export", "html", "--project", str(root))[0] == 0
        for path in sorted((root / "export").glob("*.html")):
            html = path.read_text(encoding="utf-8")
            assert 'src="http' not in html and "@import" not in html and "<script" not in html
            assert '<link rel="stylesheet"' not in html and "<link" not in html and "url(" not in html
            assert (
                '<meta http-equiv="Content-Security-Policy" '
                "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            ) in html
            assert "Segoe UI" in html and "DejaVu Sans" in html and "@page { size: A4;" in html
            # единственные адреса вне якорей — ссылки на источники
            for url in re.findall(r"(?:href|src)=\"([^\"]+)\"", html):
                assert url.startswith(("#", "https://")), url


# --- связи в тексте -------------------------------------------------------------------------------


def test_unknown_number_stays_text_and_is_reported(work, cli):
    root = work()
    claims, sources = registry(root)
    r = export.render_document("x", "Известное (У-2), неизвестное (У-99) и (И-77).", claims, sources)
    assert r.unknown == ("И-77", "У-99")
    assert "У-99" in r.html and 'href="#claim-99"' not in r.html and 'href="#source-77"' not in r.html
    assert r.links == 1
    (root / "outputs" / "bad.md").write_text("# Д\n\nНомер У-99 не в реестре.\n", encoding="utf-8")
    code, out, err = cli("export", "html", "bad", "--project", str(root))
    assert code == 0, err
    assert "не найдены" in out and "У-99 (bad)" in out
    data = json.loads(cli("export", "html", "bad", "--project", str(root), "--json")[1])
    assert data["ok"] is True and data["unknown"] == {"bad": ["У-99"]}


def test_number_in_code_is_not_a_link(work):
    root = work()
    claims, sources = registry(root)
    text = "Код `У-2` и\n\n```\nУ-4 и И-1\n```\n\n    У-3 в блоке с отступом\n\nтекст (У-2)\n"
    r = export.render_document("x", text, claims, sources)
    assert "<code>У-2</code>" in r.html
    assert "У-4 и И-1" in r.html and "У-3 в блоке с отступом" in r.html
    assert r.claims == ("У-2",) and r.links == 1 and r.unknown == ()
    body = r.html.split('<section class="grounds"')[0]
    assert body.count('href="#claim-2"') == 1 and 'href="#claim-4"' not in body


def test_number_inside_existing_link_and_glued_ids_are_untouched(work):
    root = work()
    claims, sources = registry(root)
    text = "[см. У-2](https://example.com/a) и ПП-2, ВП-2, Вар-2, У-2-й, И-1а\n"
    r = export.render_document("x", text, claims, sources)
    assert r.links == 0 and r.unknown == ()
    assert '<a href="https://example.com/a">см. У-2</a>' in r.html
    assert "<a href" not in r.html.replace('<a href="https://example.com/a">', "")


@pytest.mark.parametrize(
    "text",
    [
        "оценка У-2.5 пунктов",
        "оценка У-2,5 пункта",
        "адрес https://x.example/У-2 в тексте",
        "адрес https://x.example/?id=У-2&a=1 в тексте",
        "путь inputs/У-2",
        "файл.У-2",
    ],
)
def test_number_followed_by_fraction_or_inside_address_is_not_a_link(work, text):
    claims, sources = registry(work())
    r = export.render_document("x", text, claims, sources)
    assert r.links == 0 and "<a " not in r.html and r.unknown == ()


def test_number_before_sentence_punctuation_is_still_a_link(work):
    claims, sources = registry(work())
    r = export.render_document("x", "Цена (У-2), прогноз У-4. Источники: И-1, И-2; У-3.", claims, sources)
    assert r.claims == ("У-2", "У-3", "У-4") and r.sources == ("И-1", "И-2") and r.links == 5


def test_numbers_in_tables_headings_and_lists_become_links(work):
    root = work()
    claims, sources = registry(root)
    text = "## Цена (У-2)\n\n- пункт (И-1)\n\n| а | б |\n|---|---|\n| У-4 | **У-3** |\n"
    r = export.render_document("x", text, claims, sources)
    assert r.claims == ("У-2", "У-3", "У-4") and r.sources == ("И-1",) and r.links == 4
    assert "<table>" in r.html
    check_links(r.html)


def test_caveats_dispute_self_reported_and_suspicious(work):
    from strateg.models import Dispute

    claims, sources = registry(work())
    plain = export.render_document("x", "(У-4)", claims, sources).html
    assert "Оговорки" not in plain  # у У-4 цитата из независимого источника

    claim = claims["У-2"].model_copy(update={"dispute": Dispute(claim="У-4", reason="цифры расходятся <b>")})
    shady = {**sources, "И-2": sources["И-2"].model_copy(update={"suspicious": True})}
    html = export.render_document("x", "(У-2)", {**claims, "У-2": claim}, shady).html
    row = re.search(r"<dt>Оговорки</dt><dd>(.*?)</dd>", html).group(1)
    assert "спор: цифры расходятся &lt;b&gt; (см. У-4)" in row
    assert "источник пишет о себе" in row and "источник помечен подозрительным" in row
    assert html.count("<dt>Оговорки</dt>") == 1 and "Происхождение" not in html


# --- команда --------------------------------------------------------------------------------------


def test_cli_default_is_all_documents_into_export_folder(work, cli):
    root = work()
    before = tree_hashes(root)
    code, out, err = cli("export", "html", "--project", str(root))
    assert code == 0, err
    names = sorted(p.name for p in (root / "export").iterdir())
    assert names == sorted(f"{d}.html" for d in DOCS)  # ни временных файлов, ни PDF
    assert "Ссылок на основания: 14." in out and str(root / "export") in out
    assert tree_hashes(root) == {
        **before,
        **{k: v for k, v in tree_hashes(root).items() if k.startswith("export/")},
    }
    # проект остаётся в порядке: `check` папку export/ не трогает
    code, out, err = cli("check", "--project", str(root))
    assert code == 0 and "ошибок нет" in out, err


def test_cli_document_names_with_and_without_suffix(work, cli):
    root = work()
    assert cli("export", "html", "one-pager", "situation.md", "one-pager", "--project", str(root))[0] == 0
    assert sorted(p.name for p in (root / "export").iterdir()) == ["one-pager.html", "situation.html"]
    code, _, err = cli("export", "html", "nope", "--project", str(root))
    assert code == 2 and "нет документа «nope»" in err and "one-pager" in err
    code, _, err = cli("export", "html", "..\\state", "--project", str(root))
    assert code == 2


def test_cli_rewrites_files_in_place_and_leaves_no_temp_files(work, cli):
    root = work()
    for _ in range(2):
        assert cli("export", "html", "one-pager", "--project", str(root))[0] == 0
    assert [p.name for p in (root / "export").iterdir()] == ["one-pager.html"]


def test_cli_project_without_documents(work, cli):
    root = work()
    shutil.rmtree(root / "outputs")
    code, _, err = cli("export", "html", "--project", str(root))
    assert code == 1 and "нет документов" in err


def test_cli_cyrillic_and_space_in_every_path(work, cli, tmp_path):
    root = work()
    out = tmp_path / "Мой экспорт" / "для руководства"
    code, text, err = cli("export", "html", "comms-strategy", "--project", str(root), "--out", str(out))
    assert code == 0, err
    html = (out / "comms-strategy.html").read_bytes()
    assert (
        "Коммуникационная стратегия" in html.decode("utf-8")
        and b"\r\n" not in html
        and not html.startswith(b"\xef\xbb\xbf")
    )
    assert str(out) in text


def test_cli_json_shape(work, cli):
    root = work()
    data = json.loads(cli("export", "html", "one-pager", "--project", str(root), "--json")[1])
    doc = data["documents"][0]
    assert data["ok"] is True and data["links"] == 2 and data["unknown"] == {}
    assert doc["name"] == "one-pager" and doc["pdf"] is None and doc["claims"] == ["У-2", "У-4"]
    assert doc["sources"] == [] and doc["html"].endswith("one-pager.html")


def test_cli_help_is_in_russian(cli):
    code, out, err = cli("export", "pdf", "--help")
    assert code == 0 and "--browser" in out + err
    assert "usage" in (out + err).lower()
    code, out, err = cli("export", "--help")
    assert code == 0 and "html" in out + err and "pdf" in out + err


# --- нет браузера ---------------------------------------------------------------------------------


def test_no_browser_flag_to_missing_file(work, cli, tmp_path):
    root = work()
    missing = tmp_path / "нет такого" / "browser.exe"
    code, out, err = cli("export", "pdf", "one-pager", "--project", str(root), "--browser", str(missing))
    assert code == 1
    assert (root / "export" / "one-pager.html").is_file() and not list((root / "export").glob("*.pdf"))
    assert str(root / "export" / "one-pager.html") in err
    assert "вручную" in err and "Ctrl+P" in err and str(missing) in err
    # человек сам назвал браузер: совет «укажите --browser» ему не повторяется
    assert "укажите браузер" not in err
    # итог команды виден и без браузера
    assert "Ссылок на основания: 2." in out and "one-pager.md -> one-pager.html" in out


def test_no_browser_installed(work, cli, monkeypatch):
    root = work()
    monkeypatch.setattr(export, "browser_candidates", lambda *a, **k: [])
    code, out, err = cli("export", "pdf", "--project", str(root))
    assert code == 1 and "Chrome или Edge" in err and "укажите браузер" in err and "--browser" in err
    assert "Ссылок на основания: 14." in out
    assert len(list((root / "export").glob("*.html"))) == len(DOCS)
    data = json.loads(cli("export", "pdf", "--project", str(root), "--json")[1])
    assert data["ok"] is False and len(data["documents"]) == len(DOCS) and data["links"] == 14
    assert all(d["pdf"] is None for d in data["documents"])


def test_no_browser_keeps_the_stale_pdf_and_names_it(work, cli, monkeypatch):
    root = work()
    (root / "export").mkdir()
    stale = root / "export" / "one-pager.pdf"
    stale.write_bytes(b"%PDF-1.4 old")
    monkeypatch.setattr(export, "browser_candidates", lambda *a, **k: [])
    code, out, err = cli("export", "pdf", "one-pager", "--project", str(root))
    assert code == 1 and stale.read_bytes() == b"%PDF-1.4 old"
    assert "прежний PDF остался от прошлой печати" in err and str(stale) in err


def test_failed_print_keeps_the_stale_pdf_and_says_so(work, cli):
    root = work()
    (root / "export").mkdir()
    stale = root / "export" / "one-pager.pdf"
    stale.write_bytes(b"%PDF-1.4 old")
    # python не умеет печатать в PDF: завершается без файла
    code, out, err = cli("export", "pdf", "one-pager", "--project", str(root), "--browser", sys.executable)
    assert code == 1 and stale.read_bytes() == b"%PDF-1.4 old"
    assert "one-pager:" in err and "PDF не создан" in err and "прежний PDF остался от прошлой печати" in err
    assert (root / "export" / "one-pager.html").is_file() and "Ссылок на основания: 2." in out


def test_browser_flag_accepts_a_program_name_from_path(tmp_path, monkeypatch):
    fake = tmp_path / "mybrowser.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "mybrowser" else None)
    assert export.find_browser("mybrowser") == fake
    assert export.find_browser("other") is None


def test_broken_registry_is_a_clean_error(work, cli):
    for name, old, new in (
        ("claims.yaml", "status: действует", "status: непонятно"),
        ("sources.yaml", "kind: страница", "kind: ерунда"),
    ):
        root = work()
        path = root / name
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        code, out, err = cli("export", "html", "--project", str(root))
        assert code == 1 and "не читаются" in err and "Traceback" not in err, err
        assert not (root / "export").exists()
        data = json.loads(cli("export", "html", "--project", str(root), "--json")[1])
        assert data["ok"] is False and "не читаются" in data["error"]
        shutil.rmtree(root.parent)


def test_browser_candidates_by_platform(tmp_path):
    win = export.browser_candidates(
        {"ProgramFiles": "C:/P", "ProgramFiles(x86)": "C:/P86", "LOCALAPPDATA": "C:/L"}, "win32"
    )
    names = [p.as_posix() for p in win]
    assert names[0] == "C:/P/Microsoft/Edge/Application/msedge.exe"
    assert "C:/L/Google/Chrome/Application/chrome.exe" in names and len(names) == 6
    mac = [p.as_posix() for p in export.browser_candidates({}, "darwin")]
    assert "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" in mac
    assert export.find_browser(None, lambda: []) is None
    fake = tmp_path / "chrome"
    fake.write_text("", encoding="utf-8")
    assert export.find_browser(str(fake)) == fake
    assert export.find_browser(None, lambda: [tmp_path / "нет", fake]) == fake


def test_print_failure_is_a_clear_error_and_leaves_nothing(tmp_path, monkeypatch):
    own = tmp_path / "своя временная"  # общий %TEMP% не смотрим: в нём идут чужие экспорты
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))
    page = tmp_path / "a.html"
    page.write_text("<!doctype html><title>x</title>", encoding="utf-8")
    pdf = tmp_path / "a.pdf"
    # python не умеет печатать в PDF: завершается без файла
    with pytest.raises(export.PrintError, match="PDF не создан"):
        export.print_pdf(Path(sys.executable), page, pdf, timeout=30)
    assert not pdf.exists()
    assert list(own.iterdir()) == []  # профиль браузера и временный файл убраны


def test_print_timeout_kills_the_browser(tmp_path, monkeypatch):
    own = tmp_path / "своя временная"
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    page = tmp_path / "a.html"
    page.write_text("<!doctype html><title>x</title>", encoding="utf-8")
    # запуск `python --headless …` сразу падает, поэтому подменяем программу обёрткой
    runner = tmp_path / ("run.cmd" if sys.platform == "win32" else "run.sh")
    if sys.platform == "win32":
        runner.write_text(f'@"{sys.executable}" "{sleeper}"\n', encoding="utf-8")
    else:
        runner.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{sleeper}"\n', encoding="utf-8")
        runner.chmod(0o755)
    with pytest.raises(export.PrintError, match="остановлен"):
        export.print_pdf(runner, page, tmp_path / "a.pdf", timeout=2)
    assert not (tmp_path / "a.pdf").exists() and list(own.iterdir()) == []


# --- PDF настоящим браузером ----------------------------------------------------------------------


def _pdf_links(path: Path) -> tuple[int, int]:
    """Число ссылок-переходов внутри документа и внешних адресов во всём PDF."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(str(path))
    inner = outer = 0
    for i in range(len(pdf)):
        page = pdf[i]
        pos, link = ctypes.c_int(0), raw.FPDF_LINK()
        while raw.FPDFLink_Enumerate(page, pos, link):
            if raw.FPDFLink_GetDest(pdf, link):
                inner += 1
            elif raw.FPDFLink_GetAction(link) and raw.FPDFAction_GetType(raw.FPDFLink_GetAction(link)) == 3:
                outer += 1
    return inner, outer


@pytest.fixture
def browser():
    found = export.find_browser()
    if found is None:
        pytest.skip("Chrome или Edge не найден: печать PDF не проверить")
    return found


def test_pdf_has_cyrillic_tables_and_internal_links(work, cli, browser):
    import pypdfium2 as pdfium

    root = work()
    code, out, err = cli("export", "pdf", "one-pager", "comms-strategy", "--project", str(root))
    assert code == 0, err
    folder = root / "export"
    assert sorted(p.name for p in folder.iterdir()) == [
        "comms-strategy.html",
        "comms-strategy.pdf",
        "one-pager.html",
        "one-pager.pdf",
    ]
    one = folder / "one-pager.pdf"
    assert one.stat().st_size > 1000
    pdf = pdfium.PdfDocument(str(one))
    text = "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    for word in ("Кратко", "Одна страница для решения", "Рекомендуем Вар-2", "«Защита»", "—"):
        assert word in text
    assert "�" not in text
    inner, outer = _pdf_links(one)
    assert inner >= 2  # У-2 и У-4 в тексте ведут к основаниям
    assert outer >= 1  # адрес источника
    tables = pdfium.PdfDocument(str(folder / "comms-strategy.pdf"))
    assert len(tables) >= 1
    assert pdfium.PdfDocument(str(one))[0].get_size()[0] == pytest.approx(595, abs=3)  # A4


def test_pdf_into_cyrillic_folder_with_space(work, cli, browser, tmp_path):
    root = work("project_follow_up")
    out = tmp_path / "Моя папка" / "pdf"
    code, text, err = cli("export", "pdf", "situation", "--project", str(root), "--out", str(out))
    assert code == 0, err
    assert (out / "situation.pdf").stat().st_size > 1000 and (out / "situation.html").is_file()
    assert not [p for p in out.iterdir() if p.suffix == ".tmp"]
    assert "situation.html, situation.pdf" in text
