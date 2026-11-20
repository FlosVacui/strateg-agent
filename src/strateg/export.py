"""Экспорт документов `outputs/*.md` в HTML и PDF (ADR-0014).

HTML собирается из Markdown библиотекой markdown-it-py: набор `commonmark`, сырой HTML запрещён, таблицы
включены. Номера `У-n` и `И-n` в тексте становятся ссылками на якоря `claim-<n>` и `source-<n>` приложения
«Основания» того же документа. Ссылки ставятся по токенам разбора, а не по готовому HTML: код и уже готовые
ссылки остаются как были. Цитаты и поля реестра попадают в страницу только через `html.escape`.

PDF — печать этого HTML установленным Chrome или Edge (`--headless --print-to-pdf`). Свой браузер CLI не
скачивает: нет браузера — остаётся HTML и подсказка, как напечатать его вручную.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from strateg import commands
from strateg import project as proj
from strateg import storage
from strateg.models import Claim, ClaimsFile, Source, SourcesFile
from strateg.project import CliError, UsageError

OUTPUTS_DIR = "outputs"
DEFAULT_OUT = "export"
PRINT_TIMEOUT = 60  # секунд на печать одного документа
FORMATS = ("html", "pdf")

CLAIM_PREFIX, SOURCE_PREFIX = "У", "И"
CLAIM_ANCHOR, SOURCE_ANCHOR = "claim", "source"
# Номер не склеен с буквой или дефисом слева и справа: «ВП-1», «ПП-5», «У-3-й» ссылками не становятся.
_REF = re.compile(r"(?<![\w-])([УИ])-([1-9][0-9]*)(?![\w-])")

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0 auto; padding: 24px 20px 48px; max-width: 54rem; color: #1b1b1f; background: #fff;
  font: 11pt/1.5 "Segoe UI", Arial, "DejaVu Sans", sans-serif; overflow-wrap: anywhere;
}
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 1rem; }
h2 { font-size: 1.3rem; margin: 1.8rem 0 .6rem; border-bottom: 1px solid #c8c8d0; padding-bottom: .2rem; }
h3 { font-size: 1.1rem; margin: 1.2rem 0 .4rem; }
h1, h2, h3, h4 { break-after: avoid; }
p, ul, ol { margin: .5rem 0; }
code, pre { font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: .92em; }
pre { background: #f3f3f6; border: 1px solid #d8d8e0; padding: .6rem .8rem; white-space: pre-wrap; }
blockquote { margin: .6rem 0; padding: .1rem 1rem; border-left: 4px solid #c8c8d0; color: #3a3a44; }
a { color: #0b4fb3; text-decoration: underline; }
a.ref { font-weight: 600; white-space: nowrap; }
table { border-collapse: collapse; width: 100%; margin: .8rem 0; }
th, td { border: 1px solid #7a7a86; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #ececf1; }
tr { break-inside: avoid; }
section.grounds { margin-top: 2.5rem; border-top: 2px solid #1b1b1f; }
article.ground { border: 1px solid #c8c8d0; border-radius: 4px; padding: .5rem .9rem; margin: .8rem 0;
  break-inside: avoid; }
article.ground h3 { margin: 0 0 .3rem; }
article.ground.replaced, article.ground.revoked { border-style: dashed; background: #fbf6ea; }
.tag { font-size: .85rem; font-weight: 400; color: #4a4a55; }
.tag.alert { color: #8a2a00; font-weight: 600; }
dl { margin: .3rem 0 0; display: grid; grid-template-columns: 9rem 1fr; gap: .15rem .8rem; }
dt { color: #4a4a55; }
dd { margin: 0; }
.quote { margin: 0 0 .3rem; }
.note { color: #4a4a55; font-size: .95rem; }
@page { size: A4; margin: 16mm 15mm 18mm; }
@media print {
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  a, a.ref { color: #0b4fb3; text-decoration: underline; }
}
@media (max-width: 40rem) { dl { grid-template-columns: 1fr; } }
"""


# ---------------------------------------------------------------------------
# Разбор Markdown и связи
# ---------------------------------------------------------------------------


def make_parser() -> MarkdownIt:
    """Разбор по ADR-0014: commonmark, сырой HTML запрещён (виден как текст), таблицы включены."""
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def claim_anchor(claim_id: str) -> str:
    """Якорь утверждения: «У-12» -> «claim-12» (ASCII: кириллица в `id` ломает переходы в части просмотрщиков)."""
    return f"{CLAIM_ANCHOR}-{claim_id.split('-', 1)[1]}"


def source_anchor(source_id: str) -> str:
    return f"{SOURCE_ANCHOR}-{source_id.split('-', 1)[1]}"


def anchor_of(ref: str) -> str:
    return claim_anchor(ref) if ref.startswith(CLAIM_PREFIX + "-") else source_anchor(ref)


def _text(content: str) -> Token:
    return Token("text", "", 0, content=content)


def _ref_link(ref: str) -> list[Token]:
    opening = Token("link_open", "a", 1, attrs={"href": "#" + anchor_of(ref), "class": "ref"})
    return [opening, _text(ref), Token("link_close", "a", -1)]


@dataclass
class _Refs:
    known_claims: set[str]
    known_sources: set[str]
    claims: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)
    links: int = 0

    def known(self, ref: str) -> bool:
        return ref in (self.known_claims if ref.startswith(CLAIM_PREFIX + "-") else self.known_sources)

    def note(self, ref: str) -> None:
        (self.claims if ref.startswith(CLAIM_PREFIX + "-") else self.sources).add(ref)
        self.links += 1


def _link_text(content: str, refs: _Refs) -> list[Token]:
    """Текстовый токен -> текст и ссылки; номера не из реестра остаются текстом и попадают в `unknown`."""
    out: list[Token] = []
    pos = 0
    for m in _REF.finditer(content):
        ref = f"{m.group(1)}-{m.group(2)}"
        if not refs.known(ref):
            refs.unknown.add(ref)
            continue
        if m.start() > pos:
            out.append(_text(content[pos : m.start()]))
        out.extend(_ref_link(ref))
        refs.note(ref)
        pos = m.end()
    if pos < len(content):
        out.append(_text(content[pos:]))
    return out or [_text(content)]


def _inline(children: list[Token], refs: _Refs) -> list[Token]:
    out: list[Token] = []
    depth = 0  # внутри готовой ссылки номера не трогаем
    for tok in children:
        if tok.type == "link_open":
            depth += 1
        elif tok.type == "link_close":
            depth -= 1
        if tok.type == "image":
            # Картинки не грузятся (CSP): остаётся подпись, ни адреса, ни файла в выходе нет.
            alt = tok.content.strip()
            out.append(_text(f"[изображение: {alt}]" if alt else "[изображение]"))
        elif tok.type == "text" and depth == 0:
            out.extend(_link_text(tok.content, refs))
        else:
            out.append(tok)
    return out


def _title(tokens: list[Token], fallback: str) -> str:
    for i, tok in enumerate(tokens[:-1]):
        if tok.type == "heading_open" and tok.tag == "h1" and tokens[i + 1].type == "inline":
            text = tokens[i + 1].content.strip()
            if text:
                return text
    return fallback


# ---------------------------------------------------------------------------
# Приложение «Основания»
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _num(ref: str) -> int:
    return int(ref.split("-", 1)[1])


def _date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")


def _ref_html(ref: str) -> str:
    return f'<a class="ref" href="#{anchor_of(ref)}">{_esc(ref)}</a>'


def _row(label: str, value_html: str) -> str:
    return f"<dt>{_esc(label)}</dt><dd>{value_html}</dd>"


def _quote_html(claim: Claim, sources: dict[str, Source]) -> list[str]:
    rows = []
    for q in claim.quotes:
        parts = [f'<p class="quote">«{_esc(q.text)}»</p>']
        meta = [_ref_html(q.source) if q.source in sources else _esc(q.source)]
        if q.locator:
            meta.append(_esc(q.locator))
        if q.publisher:
            who = _esc(q.publisher)
            meta.append(f"{who} ({_esc(q.publisher_kind)})" if q.publisher_kind else who)
        if q.check is not None:
            meta.append(f"проверена: {_esc(q.check.method)}, {_date(q.check.checked_at)}")
        else:
            meta.append("не проверена")
        parts.append(f'<p class="note">{"; ".join(meta)}</p>')
        rows.append("".join(parts))
    return rows


def _status_html(claim: Claim) -> tuple[str, str]:
    """Метка статуса и класс статьи; заменённое и отозванное видно сразу."""
    if claim.status == "заменено":
        tail = f" на {_ref_html(claim.replaced_by)}" if claim.replaced_by else ""
        return f'<span class="tag alert">заменено{tail}</span>', " replaced"
    if claim.status == "отозвано":
        return '<span class="tag alert">отозвано</span>', " revoked"
    return f'<span class="tag">{_esc(claim.status)}</span>', ""


def _claim_html(claim: Claim, sources: dict[str, Source]) -> str:
    status, cls = _status_html(claim)
    rows = []
    if claim.status == "отозвано":
        reason = next((h.reason for h in reversed(claim.status_history) if h.reason), None)
        if reason:
            rows.append(_row("Причина отзыва", _esc(reason)))
    if claim.region or claim.period:
        where = "; ".join(x for x in (claim.region, claim.period) if x)
        label = "Регион и период" if claim.region and claim.period else "Регион" if claim.region else "Период"
        rows.append(_row(label, _esc(where)))
    if claim.forecast_range is not None:
        r = claim.forecast_range
        rows.append(_row("Диапазон", _esc(f"{r.low:g}–{r.high:g} {r.unit}")))
    if claim.check_method:
        rows.append(_row("Как проверить", _esc(claim.check_method)))
    if claim.taken_from:
        rows.append(_row("Откуда взято", _esc(claim.taken_from)))
    quotes = _quote_html(claim, sources)
    if quotes:
        rows.append(_row("Цитата" if len(quotes) == 1 else "Цитаты", "".join(quotes)))
    elif claim.type == "факт":
        rows.append(_row("Цитата", "нет"))
    body = f"<dl>{''.join(rows)}</dl>" if rows else ""
    return (
        f'<article class="ground{cls}" id="{claim_anchor(claim.id)}">'
        f"<h3>{_esc(claim.id)} <span class=\"tag\">{_esc(claim.type)}</span> {status}</h3>"
        f"<p>{_esc(claim.text)}</p>{body}</article>"
    )


def _source_html(src: Source) -> str:
    rows = [_row("Вид", _esc(src.kind))]
    if src.url:
        rows.append(_row("Адрес", f'<a href="{_esc(src.url)}">{_esc(src.url)}</a>'))
    if src.path:
        rows.append(_row("Файл", _esc(src.path)))
    if src.fetch is not None:
        rows.append(_row("Дата обращения", _date(src.fetch.accessed_at)))
    if src.rechecked_at is not None:
        rows.append(_row("Перепроверено", _date(src.rechecked_at)))
    if src.access_note:
        rows.append(_row("Доступ", _esc(src.access_note)))
    return (
        f'<article class="ground" id="{source_anchor(src.id)}">'
        f"<h3>{_esc(src.id)}</h3><dl>{''.join(rows)}</dl></article>"
    )


def _used_claims(
    cited: set[str], claims: dict[str, Claim]
) -> list[Claim]:
    """Утверждения приложения: упомянутые в документе и, чтобы переход не обрывался, заменившие их."""
    chosen: set[str] = set()
    todo = sorted(cited)
    while todo:
        cid = todo.pop()
        claim = claims.get(cid)
        if claim is None or cid in chosen:
            continue
        chosen.add(cid)
        if claim.replaced_by:
            todo.append(claim.replaced_by)
    return [claims[c] for c in sorted(chosen, key=_num)]


def _appendix(
    cited_claims: set[str],
    cited_sources: set[str],
    claims: dict[str, Claim],
    sources: dict[str, Source],
) -> str:
    used = _used_claims(cited_claims, claims)
    need_sources = set(cited_sources)
    for claim in used:
        need_sources.update(q.source for q in claim.quotes if q.source in sources)
    parts = []
    if used:
        parts.append(
            '<section class="grounds" id="osnovaniya"><h2>Основания</h2>'
            + "".join(_claim_html(c, sources) for c in used)
            + "</section>"
        )
    if need_sources:
        ordered = [sources[s] for s in sorted(need_sources, key=_num) if s in sources]
        parts.append(
            f'<section class="grounds{"" if used else " only"}" id="istochniki"><h2>Источники</h2>'
            + "".join(_source_html(s) for s in ordered)
            + "</section>"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Сборка страницы
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    name: str  # имя документа без .md
    title: str
    html: str
    links: int  # ссылок на основания в тексте
    claims: tuple[str, ...]
    sources: tuple[str, ...]
    unknown: tuple[str, ...]  # номера, которых нет в реестре: остались текстом


def page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_esc(CSP)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n<style>{CSS}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def render_document(
    name: str, text: str, claims: dict[str, Claim], sources: dict[str, Source], md: MarkdownIt | None = None
) -> Rendered:
    """HTML одного документа. `claims` и `sources` — реестр проекта: что в нём есть, то и становится ссылкой."""
    md = md or make_parser()
    tokens = md.parse(text)
    refs = _Refs(set(claims), set(sources))
    for tok in tokens:
        if tok.type == "inline" and tok.children:
            tok.children = _inline(tok.children, refs)
    body = md.renderer.render(tokens, md.options, {})
    appendix = _appendix(refs.claims, refs.sources, claims, sources)
    title = _title(tokens, name)
    return Rendered(
        name=name,
        title=title,
        html=page(title, body + appendix),
        links=refs.links,
        claims=tuple(sorted(refs.claims, key=_num)),
        sources=tuple(sorted(refs.sources, key=_num)),
        unknown=tuple(sorted(refs.unknown, key=lambda r: (r[0], _num(r)))),
    )


# ---------------------------------------------------------------------------
# Браузер и печать PDF
# ---------------------------------------------------------------------------


class PrintError(Exception):
    """Печать не вышла: причина для человека."""


def browser_candidates(env: dict[str, str] | None = None, platform: str | None = None) -> list[Path]:
    """Известные места Edge и Chrome по платформе: Edge первым (на Windows 11 он есть всегда)."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform == "win32":
        bases = [env.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        tails = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")
        return [Path(b) / t for t in tails for b in bases if b]
    if platform == "darwin":
        apps = (
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
        )
        return [Path("/Applications") / a for a in apps]
    found = (shutil.which(n) for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"))
    return [Path(p) for p in found if p]


def find_browser(explicit: str | None = None, candidates: Callable[[], list[Path]] | None = None) -> Path | None:
    """Путь к браузеру: флаг `--browser`, затем известные места. Флаг на несуществующий файл — None, а не
    молчаливый переход к другому браузеру: человек назвал свой."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for path in (candidates or browser_candidates)():
        if path.is_file():
            return path
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False, timeout=15
        )
    proc.kill()


def _remove_tree(path: Path) -> None:
    """Профиль браузера удаляется с повторами: на Windows дочерние процессы отпускают файлы не сразу."""
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.25)


def print_pdf(browser: Path, html_file: Path, pdf_file: Path, timeout: float = PRINT_TIMEOUT) -> None:
    """Печатает `html_file` в `pdf_file`. Результат появляется атомарно: сначала временный файл во временной
    папке (путь ASCII, если система позволяет), затем перенос на место."""
    work = Path(tempfile.mkdtemp(prefix="strateg-export-"))
    try:
        profile = work / "profile"
        raw = work / "out.pdf"
        args = [
            str(browser),
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={raw}",
            "--no-pdf-header-footer",
            html_file.resolve().as_uri(),
        ]
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise PrintError(f"браузер {browser} не запустился: {exc}") from exc
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            raise PrintError(f"браузер не напечатал PDF за {timeout:g} с и был остановлен") from None
        deadline = time.monotonic() + 5  # файл может появиться чуть позже конца процесса
        while not (raw.is_file() and raw.stat().st_size > 0) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not (raw.is_file() and raw.stat().st_size > 0):
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            hint = f": {tail[0]}" if tail else ""
            raise PrintError(f"браузер завершился (код {proc.returncode}), но PDF не создан{hint}")
        pdf_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = storage.temp_name(pdf_file)
        try:
            shutil.copyfile(raw, tmp)
            storage.replace_file(tmp, pdf_file)
        except (OSError, storage.FileBusy) as exc:
            tmp.unlink(missing_ok=True)
            raise PrintError(f"не удалось записать {pdf_file}: {exc}") from exc
    finally:
        _remove_tree(work)


# ---------------------------------------------------------------------------
# Команда
# ---------------------------------------------------------------------------


def _documents(root: Path, names: list[str]) -> list[Path]:
    folder = root / OUTPUTS_DIR
    existing = sorted(p for p in folder.glob("*.md") if p.is_file()) if folder.is_dir() else []
    if not names:
        if not existing:
            raise CliError(f"в {OUTPUTS_DIR}/ проекта {root.name} нет документов .md: экспортировать нечего")
        return existing
    by_name = {p.stem: p for p in existing}
    out: list[Path] = []
    for raw in names:
        stem = raw.replace("\\", "/")
        if stem.startswith(OUTPUTS_DIR + "/"):
            stem = stem[len(OUTPUTS_DIR) + 1 :]
        stem = stem[:-3] if stem.endswith(".md") else stem
        if "/" in stem or stem not in by_name:
            have = ", ".join(by_name) or "документов нет"
            raise UsageError(f"в {OUTPUTS_DIR}/ нет документа «{raw}»; есть: {have}")
        if by_name[stem] not in out:
            out.append(by_name[stem])
    return out


def _registry(root: Path) -> tuple[dict[str, Claim], dict[str, Source]]:
    claims = ClaimsFile.model_validate(proj.read_yaml(root / "claims.yaml"))
    sources = SourcesFile.model_validate(proj.read_yaml(root / "sources.yaml"))
    return {c.id: c for c in claims.claims}, {s.id: s for s in sources.sources}


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"документ {path.name} не читается как текст UTF-8") from exc


def _no_browser_details(html_files: list[Path], browser: str | None) -> list[str]:
    named = f"браузер {browser} не найден" if browser else "не найден установленный Chrome или Edge"
    return [
        named + "; PDF не создан",
        *[f"HTML собран: {p}" for p in html_files],
        "Откройте HTML в браузере и напечатайте в PDF вручную: Ctrl+P, «Сохранить как PDF», "
        "колонтитулы выключите",
        "Или укажите браузер: `strateg export pdf --browser <путь к chrome или msedge>`",
    ]


def cmd_export(args: argparse.Namespace) -> int:
    fmt = args.format
    root = proj.resolve_project(getattr(args, "project", None))
    docs = _documents(root, list(args.documents))
    claims, sources = _registry(root)
    out_dir = Path(args.out) if args.out else root / DEFAULT_OUT
    md = make_parser()

    rendered = [render_document(p.stem, _read(p), claims, sources, md) for p in docs]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        html_files = []
        for r in rendered:
            target = out_dir / f"{r.name}.html"
            proj.write_text(target, r.html)
            html_files.append(target)
    except OSError as exc:
        raise CliError(f"не удалось записать в {out_dir}: {exc}") from exc

    pdf_files: list[Path] = []
    pdf_errors: list[str] = []
    if fmt == "pdf":
        browser = find_browser(args.browser)
        if browser is None:
            return _fail_no_browser(args, html_files, args.browser)
        for r, html_file in zip(rendered, html_files, strict=True):
            pdf = out_dir / f"{r.name}.pdf"
            try:
                print_pdf(browser, html_file, pdf)
                pdf_files.append(pdf)
            except PrintError as exc:
                pdf_errors.append(f"{r.name}: {exc}")

    unknown = {r.name: list(r.unknown) for r in rendered if r.unknown}
    data: dict[str, Any] = {
        "out": str(out_dir),
        "documents": [
            {
                "name": r.name,
                "title": r.title,
                "html": str(out_dir / f"{r.name}.html"),
                "pdf": str(out_dir / f"{r.name}.pdf") if out_dir / f"{r.name}.pdf" in pdf_files else None,
                "links": r.links,
                "claims": list(r.claims),
                "sources": list(r.sources),
            }
            for r in rendered
        ],
        "links": sum(r.links for r in rendered),
        "unknown": unknown,
    }
    lines = [f"Проект {root.name}: экспорт {'HTML и PDF' if fmt == 'pdf' else 'HTML'}, документов {len(rendered)}."]
    for r in rendered:
        made = f"{r.name}.html" + (f", {r.name}.pdf" if out_dir / f"{r.name}.pdf" in pdf_files else "")
        lines.append(f"  {r.name}.md -> {made} (ссылок на основания: {r.links})")
    lines.append(f"Ссылок на основания: {data['links']}.")
    if unknown:
        lines.append(
            "Номера не найдены в реестре, остались текстом: "
            + "; ".join(f"{', '.join(v)} ({k})" for k, v in unknown.items())
            + "."
        )
    lines.append(f"Папка: {out_dir}")
    if pdf_errors:
        data["pdf_errors"] = pdf_errors
        return _fail_print(args, data, lines, pdf_errors)
    return commands.emit(args, data, lines)


def _fail_print(args: argparse.Namespace, data: dict[str, Any], lines: list[str], errors: list[str]) -> int:
    message = f"PDF создан не для всех документов: ошибок {len(errors)}"
    if getattr(args, "json", False):
        print(_json({"ok": False, "error": message, "details": errors, **data}))
        return 1
    for line in lines:
        print(line)
    return commands.fail(args, CliError(message, errors))


def _fail_no_browser(args: argparse.Namespace, html_files: list[Path], browser: str | None) -> int:
    details = _no_browser_details(html_files, browser)
    message = "PDF не создан: нет браузера для печати"
    if getattr(args, "json", False):
        print(_json({"ok": False, "error": message, "details": details, "html": [str(p) for p in html_files]}))
        return 1
    return commands.fail(args, CliError(message, details))


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)
