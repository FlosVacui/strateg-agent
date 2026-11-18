"""Материалы человека: извлечение текста и регистрация источника (ADR-0004, ADR-0011).

`strateg add <путь>` берёт файл из `inputs/`, достаёт текст (с метками для места цитаты), проверяет его без
модели и записывает источник `И-n`:
- нашлись персональные данные: модель видит только обезличенную копию (`inputs/i-<n>.txt`), исходник
  уходит в `.tmp/pending/И-n/` и в работу не идёт; после `add --confirm И-n` он переносится в
  `~/.strateg/private/<проект>/И-n/` — вне папки проекта и вне архива;
- изображение: текста нет, человек подтверждает, что персональных данных на нём нет (`add --confirm`);
- закрытые данные и обращения к ИИ: те же признаки и события, что у `fetch`;
- имена людей — только предупреждение;
- аудио и видео не разбираются.

Метки места цитаты в тексте: `[стр. N]` (PDF), `[абз. N]`, `[таблица N]`, `[колонтитул]` (DOCX),
`[лист: имя]` и адреса ячеек `B2:` (XLSX и CSV), `[слайд N]` (PPTX), `[строка N]` (расшифровка интервью).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from strateg import fetch as fetchlib
from strateg import project as proj
from strateg import safety
from strateg.models import Source
from strateg.project import CliError, Project, UsageError

AUDIO_VIDEO = frozenset(
    {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac", ".wma", ".aiff", ".amr"}
    | {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v", ".flv", ".mpg", ".mpeg", ".3gp"}
)
IMAGES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"})
KIND_BY_EXT = {
    ".txt": "заметка пользователя",
    ".md": "заметка пользователя",
    ".pdf": "PDF",
    ".docx": "документ",
    ".xlsx": "таблица",
    ".csv": "таблица",
    ".pptx": "презентация",
    **dict.fromkeys(IMAGES, "изображение"),
}
USER_KINDS = ("заметка пользователя", "PDF", "документ", "таблица", "презентация", "изображение", "интервью")
TEXT_LIKE = (".txt", ".md", ".docx", ".pdf")  # расшифровку интервью можно дать в любом из этих форматов
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 20_000_000
INPUTS = "inputs"
PENDING = ".tmp/pending"
PRIVATE_NAME = "~/.strateg/private"
NAME_CATEGORY = "имя файла"


# ---------------------------------------------------------------------------
# Извлечение текста
# ---------------------------------------------------------------------------


def _decode(body: bytes) -> str:
    return fetchlib.decode_body(body)[0]


def _squash(value: object) -> str:
    return " ".join(str(value).split())


def _txt(body: bytes, interview: bool) -> str:
    text = _decode(body)
    if not interview:
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(f"[строка {n}] {line}" for n, line in enumerate(lines, start=1) if line.strip())


def _docx(body: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(body))
    out: list[str] = []
    paragraphs = tables = 0
    for element in doc.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(element, doc).text.strip()
            if text:
                paragraphs += 1
                out += [f"[абз. {paragraphs}]", text]
        elif tag == "tbl":
            tables += 1
            out.append(f"[таблица {tables}]")
            for r, row in enumerate(Table(element, doc).rows, start=1):
                cells: list[str] = []
                last = None
                for cell in row.cells:
                    if cell._tc is last:  # объединённая ячейка приходит несколько раз
                        continue
                    last = cell._tc
                    cells.append(_squash(cell.text))
                if any(cells):
                    out.append(f"строка {r}: " + " | ".join(cells))
    notes = _docx_header_footer(doc)
    if notes:
        out += ["[колонтитул]", *notes]
    return "\n".join(out)


def _docx_header_footer(doc: Any) -> list[str]:
    """Тексты колонтитулов всех разделов без повторов: в них тоже бывают телефоны и почта."""
    seen: set[str] = set()
    out: list[str] = []
    for section in doc.sections:
        for part in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            if part.is_linked_to_previous:
                continue
            texts = [_squash(par.text) for par in part.paragraphs]
            for table in part.tables:
                texts += [_squash(cell.text) for row in table.rows for cell in row.cells]
            for text in texts:
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
    return out


def _cell_text(value: object) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return _squash(value)


def _xlsx(body: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            out.append(f"[лист: {ws.title}]")
            for row in ws.iter_rows():
                cells = []
                for cell in row:
                    value = getattr(cell, "value", None)
                    if value is None or (isinstance(value, str) and not value.strip()):
                        continue
                    cells.append(f"{cell.coordinate}: {_cell_text(value)}")
                if cells:
                    out.append(" | ".join(cells))
    finally:
        wb.close()
    return "\n".join(out)


def _csv(body: bytes) -> str:
    from openpyxl.utils import get_column_letter

    text = _decode(body)
    sample = text[:4096]
    delimiter = max(",;\t", key=sample.count)
    out = ["[лист: данные]"]
    for r, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        cells = [f"{get_column_letter(c)}{r}: {_squash(v)}" for c, v in enumerate(row, start=1) if v.strip()]
        if cells:
            out.append(" | ".join(cells))
    return "\n".join(out)


def _shapes(shapes):
    for shape in shapes:
        yield shape
        if getattr(shape, "shapes", None) is not None and shape.shape_type == 6:  # группа фигур
            yield from _shapes(shape.shapes)


def _pptx(body: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(body))
    out: list[str] = []
    for n, slide in enumerate(prs.slides, start=1):
        out.append(f"[слайд {n}]")
        for shape in _shapes(slide.shapes):
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = _squash(para.text)
                    if text:
                        out.append(text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for r, row in enumerate(shape.table.rows, start=1):
                    cells = [_squash(c.text) for c in row.cells]
                    if any(cells):
                        out.append(f"строка {r}: " + " | ".join(cells))
        if slide.has_notes_slide:
            notes = _squash(slide.notes_slide.notes_text_frame.text)
            if notes:
                out.append("заметки: " + notes)
    return "\n".join(out)


def _pdf(body: bytes) -> str:
    try:
        return fetchlib.extract_pdf(body)
    except fetchlib.FetchProblem as exc:
        raise CliError(
            f"PDF не открывается ({exc.note}): возможно, он защищён паролем или повреждён"
        ) from exc


_BARE_MARK = re.compile(r"^\[(?:абз\. \d+|таблица \d+|колонтитул|стр\. \d+)\]$")


def _number_lines(text: str) -> str:
    """Расшифровка в DOCX и PDF: строки с метками `[строка N]`, как у текстовой; голые метки места убраны."""
    lines = [
        x for x in text.replace("\r\n", "\n").split("\n") if x.strip() and not _BARE_MARK.match(x.strip())
    ]
    return "\n".join(f"[строка {n}] {line}" for n, line in enumerate(lines, start=1))


def extract_text(body: bytes, ext: str, kind: str) -> str:
    """Текст материала с метками места цитаты; ошибка по-русски, если файл не читается или текста нет."""
    try:
        if ext == ".pdf":
            text = _pdf(body)
        elif ext == ".docx":
            text = _docx(body)
        elif ext == ".xlsx":
            text = _xlsx(body)
        elif ext == ".csv":
            text = _csv(body)
        elif ext == ".pptx":
            text = _pptx(body)
        else:
            text = _txt(body, interview=kind == "интервью")
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - библиотеки разбора бросают разные исключения
        raise CliError(
            f"файл формата «{ext}» не читается ({type(exc).__name__}): он повреждён, защищён паролем или "
            "другого формата; сохраните его как текст, PDF, DOCX, XLSX или PPTX"
        ) from exc
    if kind == "интервью" and ext in (".docx", ".pdf"):
        text = _number_lines(text)
    if "\x00" in text[:4096]:
        raise CliError("файл двоичный, а не текстовый: дайте расшифровку или выгрузку текстом")
    if len(text) > MAX_TEXT_CHARS:
        raise CliError("в файле слишком много текста: разделите его на части")
    if not text.strip():
        raise CliError(
            "в файле нет текста"
            + (
                ": у PDF нет текстового слоя (скан), дайте расшифровку текстом или изображение"
                if ext == ".pdf"
                else ""
            )
        )
    return text


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """Итог `add`: источник и то, что нужно показать человеку."""

    source: Source
    duplicate: bool = False
    report: safety.MaterialReport | None = None
    moved: str | None = None  # куда перенесена лишняя копия исходника с персональными данными
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_confirm(self) -> bool:
        s = self.source
        return bool(s.anonymized and not s.anonymization_confirmed) or (
            s.kind == "изображение" and not s.no_personal_data_confirmed
        )


def resolve_material(p: Project, raw: str) -> Path:
    """Файл материала: путь как есть, затем от папки `inputs/` проекта и от папки проекта."""
    given = Path(raw)
    candidates = (
        [given] if given.is_absolute() else [Path.cwd() / given, p.root / INPUTS / given, p.root / given]
    )
    for c in candidates:
        if c.is_file():
            return c
    raise CliError(f"файл {raw} не найден: положите материал в папку inputs/ проекта {p.id}")


def inputs_relpath(p: Project, file: Path) -> str:
    """Путь материала от корня проекта (`inputs/…`); файл вне `inputs/` — отказ."""
    try:
        rel = file.resolve().relative_to((p.root / INPUTS).resolve())
    except ValueError:
        raise CliError(
            f"файл лежит вне папки inputs/ проекта {p.id}: положите его в {INPUTS}/ и повторите"
        ) from None
    return f"{INPUTS}/{rel.as_posix()}"


def pick_kind(ext: str, forced: str | None) -> str:
    """Вид по расширению; `--kind` допускает «интервью» для текстовых форматов и любой текстовый вид для
    незнакомого расширения."""
    default = KIND_BY_EXT.get(ext)
    if forced is None:
        if default is None:
            raise CliError(
                f"формат «{ext or 'без расширения'}» не знаком: поддерживаются .txt, .md, .pdf, .docx, "
                ".xlsx, "
                ".csv, .pptx и изображения; для текстового файла другого вида добавьте "
                '`--kind "заметка пользователя"`'
            )
        return default
    if forced not in USER_KINDS:
        raise UsageError("вид «" + forced + "» не бывает: допустимо " + ", ".join(USER_KINDS))
    if default is None:
        if forced not in ("заметка пользователя", "интервью"):
            raise UsageError("для незнакомого расширения подходят виды «заметка пользователя» и «интервью»")
        return forced
    if forced == default or (forced == "интервью" and ext in TEXT_LIKE):
        return forced
    raise UsageError(f"вид «{forced}» не подходит файлу «{ext}»: он читается как «{default}»")


def refuse_audio_video(ext: str) -> None:
    if ext in AUDIO_VIDEO:
        raise CliError(
            f"аудио и видео не разбираются: дайте расшифровку текстом (.txt, .md или .docx) "
            f"и добавьте её как обычный документ; формат «{ext}»"
        )


def pending_dir(p: Project, sid: str) -> Path:
    return p.root / PENDING / sid


def private_dir(project_id: str, sid: str) -> Path:
    """Папка исходников с персональными данными: в домашней папке пользователя, вне проекта."""
    return Path.home() / ".strateg" / "private" / project_id / sid


def private_label(project_id: str, sid: str) -> str:
    return f"{PRIVATE_NAME}/{project_id}/{sid}/"


def _unique(folder: Path, name: str) -> Path:
    target = folder / name
    n = 1
    while target.exists():
        n += 1
        target = folder / f"{Path(name).stem}.{n}{Path(name).suffix}"
    return target


def _move(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique(folder, src.name)
    shutil.move(str(src), str(target))
    return target


def _text_rel(p: Project, name: str, number: int, *, pii: bool) -> str:
    """Где лежит извлечённый текст. У материала с персональными данными имя нейтральное: в имени файла
    тоже бывают фамилии и номера.

    Занятое имя обходится номером; занятый файл не перезаписывается никогда, даже если записи о нём нет.
    """
    if pii:
        return f"{INPUTS}/i-{number}.txt"  # номер выбран мимо занятых `i-<n>.txt` (`next_source_number`)
    used = {s.text_path for s in p.sources.sources}
    rel = f"{INPUTS}/{name}.txt"
    if rel in used or (p.root / rel).exists():
        k = number
        while (p.root / f"{INPUTS}/{name}.{k}.txt").exists():
            k += 1
        rel = f"{INPUTS}/{name}.{k}.txt"
    return rel


def _known_material(p: Project, rel: str, sha: str) -> Source | None:
    """Источник с тем же файлом (sha256); текст, который CLI уже извлёк, материалом не считается."""
    for s in p.sources.sources:
        if s.text_path == rel:
            raise CliError(
                f"{rel} — текст, который CLI уже извлёк для {s.id}, а не материал: добавляйте исходные файлы"
            )
    return next((s for s in p.sources.sources if s.material_sha256 == sha), None)


CONSENT_TEMPLATE = "knowledge/templates/consent.md"
MAX_CONSENT_CHARS = 300
_CONSENT_DATE = re.compile(r"(?<![\d.])(?:\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2})(?![\d.])")


def check_consent(raw: str | None) -> tuple[str, list[str]]:
    """Отметка согласия собеседника: как и когда оно получено, без его имени и контактов.

    Без отметки расшифровка интервью в работу не идёт. Отметка хранится в реестре источников, поэтому
    персональных данных в ней быть не должно: «письменно в чате, 12.11.2026», а не «Иван, +7…».
    Возвращает отметку и предупреждения (слово латиницей с заглавной может оказаться именем).
    """
    text = " ".join((raw or "").split())
    if not text:
        raise CliError(
            "расшифровка интервью принимается только с отметкой согласия собеседника: добавьте "
            "`--consent '<как получено, дата>'`, например «письменно в чате, 12.11.2026». "
            f"Согласие получает человек до записи, образец — {CONSENT_TEMPLATE}"
        )
    if len(text) > MAX_CONSENT_CHARS:
        raise CliError(
            f"отметка согласия длиннее {MAX_CONSENT_CHARS} знаков: одна строка, как и когда получено"
        )
    if not _CONSENT_DATE.search(text):
        raise CliError(
            "в отметке согласия нет даты: напишите, как и когда оно получено, и дату в виде "
            "ДД.ММ.ГГГГ или ГГГГ-ММ-ДД, например «устно, 12.11.2026»"
        )
    if safety.mask_personal(text)[1] or safety.find_names(text):
        raise CliError(
            "в отметке согласия есть имя, телефон, почта или номер документа собеседника: "
            "в реестре они не хранятся, напишите «собеседник 1» и как получено согласие"
        )
    warnings: list[str] = []
    latin = safety.find_latin_names(text)
    if latin:
        warnings.append(
            "в отметке согласия есть слова латиницей с заглавной ("
            + ", ".join(w for w, _ in latin[:5])
            + "): если это имя собеседника, уберите его, в реестре имён быть не должно"
        )
    return text, warnings


# Расшифровка без названного вида: отказ только по сильным признакам, слабые дают предупреждение.
_STRONG_NAME = re.compile(r"расшифровк|transcript", re.IGNORECASE)
_WEAK_NAME = re.compile(r"интервью|interview|респондент|respondent", re.IGNORECASE)
_LABEL = re.compile(r"^[\s>*_#-]*([^\W\d_][^:\n]{0,19}?)[\s*_]*:(?=\s*\S)")
_DASH_LINE = re.compile(r"^\s*[—–-]\s+\S")
_PAIRS_NOT_DIALOGUE = frozenset({frozenset({"вопрос", "ответ"}), frozenset({"question", "answer"})})
_MIN_TURNS = 3  # реплик у каждого из двух собеседников для сильного признака


def _turns(text: str) -> tuple[int, int]:
    """Чередование двух коротких ярлыков реплик: (реплик у менее частого, число смен говорящего).

    Ярлык — слово или два до двоеточия в начале строки («Модератор:», «Q:», «Анна:»), цифры в нём не в счёт.
    Пары «Вопрос»/«Ответ» протокола и FAQ диалогом не считаются.
    """
    seq: list[str] = []
    for line in text.split("\n"):
        m = _LABEL.match(line)
        if m:
            seq.append(re.sub(r"[\d\s]+", " ", m.group(1)).strip().casefold())
    if len(seq) < 2 * _MIN_TURNS:
        return 0, 0
    counts: dict[str, int] = {}
    for label in seq:
        counts[label] = counts.get(label, 0) + 1
    top = sorted(counts, key=lambda k: (-counts[k], k))[:2]
    if len(top) < 2 or frozenset(top) in _PAIRS_NOT_DIALOGUE:
        return 0, 0
    pair = [x for x in seq if x in top]
    changes = sum(1 for a, b in zip(pair, pair[1:], strict=False) if a != b)
    return min(counts[top[0]], counts[top[1]]), changes


def _dash_turns(text: str) -> int:
    """Диалог через тире: наибольшее число строк-реплик подряд (по две на пару)."""
    best = run = 0
    for line in text.split("\n"):
        if _DASH_LINE.match(line):
            run += 1
            best = max(best, run)
        elif line.strip():
            run = 0
    return best


def transcript_signs(name: str, text: str, ext: str) -> tuple[bool, list[str]]:
    """Признаки расшифровки интервью без названного вида: (сильные ли, названия признаков).

    Сильные: «расшифровка» или `transcript` в имени файла; чередование двух ярлыков реплик не меньше трёх раз
    у каждого (для тире — не меньше шести строк подряд). Слабые: «интервью» и «респондент» в имени, пары
    «Вопрос»/«Ответ», короткое чередование. Отказ — только по сильным, слабые дают предупреждение.
    """
    strong: list[str] = []
    weak: list[str] = []
    if _STRONG_NAME.search(name):
        strong.append("в имени файла «расшифровка»")
    elif _WEAK_NAME.search(name):
        weak.append("в имени файла «интервью» или «респондент»")
    low, changes = _turns(text)
    if low >= _MIN_TURNS and changes >= 2 * _MIN_TURNS - 1:
        strong.append("реплики двух собеседников с ярлыками по очереди")
    elif low >= 2 and changes >= 3:
        weak.append("реплики двух собеседников по очереди")
    dash = _dash_turns(text)
    if dash >= 2 * _MIN_TURNS:
        strong.append("диалог через тире")
    elif dash >= 4:
        weak.append("диалог через тире")
    if re.search(r"^\s*(?:вопрос|ответ)\s*\d*\s*:", text, re.I | re.M):
        weak.append("пары «Вопрос»/«Ответ»")
    if ext not in TEXT_LIKE:  # таблица или презентация расшифровкой интервью не принимается
        return False, [*strong, *weak]
    return bool(strong), [*strong, *weak]


def register(p: Project, file: Path, forced_kind: str | None, consent: str | None = None) -> Outcome:
    """Регистрирует материал из `inputs/`; повтор того же файла (sha256) отдаёт прежний источник.

    Файл читается и разбирается (DOCX, PDF, проверки текста) без блокировки папки проекта. Под блокировкой —
    сверка с реестром заново (параллельная команда могла записать тот же файл), номер `И-n`, текст материала,
    запись в реестр и события (ADR-0002, «Параллельные команды»).

    Расшифровка интервью (`--kind интервью`) принимается только с отметкой согласия: она хранится у источника
    (`consent`). В её тексте заменяются и имена людей, а не только телефоны и почта.
    """
    started = time.monotonic()
    rel = inputs_relpath(p, file)
    ext = file.suffix.lower()
    refuse_audio_video(ext)
    kind = pick_kind(ext, forced_kind)
    consent_warnings: list[str] = []
    if kind == "интервью":
        consent, consent_warnings = check_consent(consent)
    elif consent is not None:
        raise UsageError("--consent относится к расшифровке интервью: добавьте `--kind интервью`")
    name_hit = bool(safety.mask_personal(file.name)[1])
    if name_hit and kind == "изображение":
        raise CliError(
            "в имени файла есть персональные данные (телефон, почта или номер документа): "
            "переименуйте файл и добавьте его снова"
        )
    try:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise CliError(f"файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ: разделите его на части")
        body = file.read_bytes()
    except FileNotFoundError:  # исходник с ПДн только что унёс сосед, который добавлял тот же файл
        moved = moved_duplicate(p, file.name)
        if moved is None:
            raise CliError(
                f"файл {file.name} исчез из inputs/ во время добавления: повторите `strateg add`"
            ) from None
        return moved
    sha = proj.sha256_bytes(body)

    twin = _known_material(p, rel, sha)  # источники не пропадают: повтор виден и без блокировки
    if twin is not None:
        return _duplicate(p, twin, file, rel)

    report: safety.MaterialReport | None = None
    text: str | None = None
    hints: list[str] = []
    if kind != "изображение":
        report = safety.inspect_material(extract_text(body, ext, kind))
        if kind == "интервью":
            # в расшифровке главное персональное данное — имя: заменяется, а не только называется.
            # Расшифровка всегда идёт путём персональных данных: словарь имён неполон, обезличенную копию
            # сверяет человек (`add --confirm`), исходник уходит из inputs/.
            masked, named = safety.mask_names(report.text)
            if named:
                report.text = masked
                report.replacements = [*report.replacements, *named]
            if safety.NAMES_CATEGORY not in report.anonymized:
                report.anonymized = [*report.anonymized, safety.NAMES_CATEGORY]
            report.names = safety.find_latin_names(report.text)  # латиница с заглавной: имена не заменены
        text = report.text
        if forced_kind is None and kind != "интервью":
            strong, signs = transcript_signs(file.name, text, ext)
            if strong:
                alt = KIND_BY_EXT.get(ext, "заметка пользователя")
                raise CliError(
                    "похоже на расшифровку интервью, а вид не назван ("
                    + "; ".join(signs)
                    + "): расшифровку принимают только с согласием собеседника, имена в ней заменяются. "
                    "Интервью — `strateg add <файл> --kind интервью --consent '<как получено, дата>'`; "
                    f'не интервью — тот же вызов с `--kind "{alt}"`'
                )
            if signs:
                hints.append(
                    "по признакам это может быть расшифровка интервью (" + "; ".join(signs) + "): "
                    "если так, добавьте файл заново с `--kind интервью --consent '<как получено, дата>'`, "
                    "иначе имена в тексте не заменяются"
                )
    pii = bool(report and report.replacements) or name_hit or kind == "интервью"

    outcome: Outcome | None = None
    with proj.transaction(p, rollback=False):
        twin = _known_material(p, rel, sha)
        if twin is not None:  # пока разбирали файл, его записала другая команда
            return _duplicate(p, twin, file, rel)
        number = proj.next_source_number(p)
        sid = f"И-{number}"
        text_rel = _text_rel(p, file.name, number, pii=pii) if text is not None else None

        data: dict[str, object] = {
            "id": sid,
            "kind": kind,
            "path": text_rel if pii else rel,
            "access": "получен",
            "untrusted": kind != "заметка пользователя",
            "material_sha256": sha,
        }
        if kind == "интервью":
            data["consent"] = consent
        if report is not None and text_rel is not None and text is not None:
            data.update(
                text_path=text_rel,
                text_sha256=proj.sha256_bytes(text.encode("utf-8")),
                suspicious=bool(report.instruction_signs),
                anonymized=[*report.anonymized, *([NAME_CATEGORY] if name_hit else [])],
                closed_markers=report.closed_markers,
            )
        try:
            source = Source.model_validate(data)
        except ValidationError as exc:
            raise CliError(
                f"материал нельзя зарегистрировать: путь «{rel}» не подходит (пробелы по краям, "
                "точка в конце имени "
                "или запрещённые знаки); переименуйте файл"
            ) from exc

        outcome = Outcome(source=source, report=report)
        original_moved: Path | None = None
        written = False
        try:
            if pii:
                original_moved = _move(file, pending_dir(p, sid))
            if text is not None and text_rel is not None:
                proj.write_text(p.root / text_rel, text)
                written = True
            p.sources.sources.append(source)
            p.save_sources()
            key = proj.key_hash("add", sha)
            p.add_event(p.event(kind="действие", name="strateg add", idempotency_hash=key, status="начато"))
            if source.suspicious:
                p.add_event(
                    p.event(
                        kind="найдена инструкция в источнике",
                        source=sid,
                        message="в тексте материала найдено обращение к ИИ, оно не исполнено (признаки: "
                        + "; ".join(report.instruction_signs if report else [])
                        + ")",
                    )
                )
            p.add_event(
                p.event(
                    kind="действие",
                    name="strateg add",
                    idempotency_hash=key,
                    status="выполнено",
                    duration_s=round(time.monotonic() - started, 2),
                    message=f"{sid}: {kind}"
                    + (
                        f", найдены персональные данные ({', '.join(source.anonymized)}): "
                        "исходник вне работы до подтверждения"
                        if pii
                        else ""
                    ),
                )
            )
            p.save_state()
        except BaseException:
            if written and text_rel:
                (p.root / text_rel).unlink(missing_ok=True)
            if original_moved is not None and not file.exists():
                shutil.move(str(original_moved), str(file))
            raise
    _warnings(outcome)
    outcome.warnings[:0] = consent_warnings
    outcome.warnings.extend(hints)
    return outcome


def moved_duplicate(p: Project, name: str) -> Outcome | None:
    """Файла в `inputs/` уже нет: его перенёс сосед (исходник с ПДн). Отдаёт прежний источник без переноса.

    Источник узнаётся по содержимому: в `.tmp/pending/` или в папке подтверждённых лежит файл с таким именем
    (перенос мог добавить к имени номер), и его sha256 равен `material_sha256` источника. Одно имя у разных
    материалов не путает их; если подходят несколько, берётся последний по номеру.
    """
    with proj.locked(p.root):
        p.refresh()
    stem, suffix = Path(name).stem, Path(name).suffix
    found: list[Source] = []
    for s in p.sources.sources:
        if not (s.anonymized and s.material_sha256):
            continue
        for folder in (pending_dir(p, s.id), private_dir(p.id, s.id)):
            candidates = [f for f in folder.glob("*") if f.is_file()] if folder.is_dir() else []
            if any(
                (f.name == name or (f.name.startswith(f"{stem}.") and f.name.endswith(suffix)))
                and proj.sha256_bytes(f.read_bytes()) == s.material_sha256
                for f in candidates
            ):
                found.append(s)
                break
    if not found:
        return None
    return Outcome(source=max(found, key=lambda x: int(x.id.split("-")[1])), duplicate=True)


def _duplicate(p: Project, twin: Source, file: Path, rel: str) -> Outcome:
    """Тот же файл уже зарегистрирован. Лишняя копия исходника с персональными данными в `inputs/` уходит
    туда же, где лежит исходник: в `inputs/` ей делать нечего."""
    outcome = Outcome(source=twin, duplicate=True)
    if twin.anonymized and twin.path != rel and file.is_file():  # исходник мог унести сосед
        if twin.anonymization_confirmed:
            _move(file, private_dir(p.id, twin.id))
            outcome.moved = private_label(p.id, twin.id)
        else:
            _move(file, pending_dir(p, twin.id))
            outcome.moved = f"{PENDING}/{twin.id}/"
        outcome.warnings.append(
            f"{rel} — копия исходника с персональными данными: перенесена в {outcome.moved}, в inputs/ её нет"
        )
    return outcome


def _warnings(outcome: Outcome) -> None:
    s, rep = outcome.source, outcome.report
    w = outcome.warnings
    if s.kind == "интервью":
        w.append(
            "в работу идёт только обезличенная копия, исходник вынесен из inputs/ и ждёт вашего "
            f"подтверждения: `strateg add --confirm {s.id}`. Сверьте копию с исходником: словарь имён "
            "неполон, он пропускает имена латиницей и строчными и принимает за имена названия сервисов "
            "и городов («Макс», «в Королёве»). Проверьте названия сервисов и городов"
        )
    elif (rep is not None and rep.replacements) or NAME_CATEGORY in s.anonymized:
        w.append(
            f"найдены {safety.found_what(s.anonymized)}: в работу идёт только обезличенная копия, исходник "
            "вынесен из inputs/ "
            f"и ждёт вашего подтверждения: `strateg add --confirm {s.id}`"
        )
    if rep is not None and rep.key_kinds:
        w.append(safety.keys_message(rep.key_kinds))
    if rep is not None and rep.names:
        w.append(
            "возможные имена людей в тексте (словарь ошибается: «Роза», «Вера»; латиницей с заглавной "
            "имена не заменяются); имена не заменены, решаете вы: "
            + ", ".join(f"{n} (строка {line})" for n, line in rep.names[:12])
            + (f" и ещё {len(rep.names) - 12}" if len(rep.names) > 12 else "")
        )
    if s.kind == "изображение":
        w.append(
            "изображение модель увидит напрямую: если на нём нет людей и контактов, подтвердите это: "
            f"`strateg add --confirm {s.id}`"
        )
    if s.closed_markers:
        w.append(
            "найдены признаки закрытых данных ("
            + ", ".join(s.closed_markers)
            + "): материал в работу не идёт, "
            f"пока вы не подтвердите, что он общедоступный: `strateg source confirm {s.id}`"
        )
    if s.suspicious:
        w.append("в тексте найдено обращение к ИИ: источник помечен «подозрительный», оно не исполняется")


def line_context(text: str, line: int, width: int = 110) -> str:
    """Строка обезличенного текста: человек видит, где стоит замена."""
    lines = text.split("\n")
    value = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return value if len(value) <= width else value[: width - 1] + "…"


def confirm(p: Project, sid: str) -> dict[str, object]:
    """Человек подтвердил замены (или то, что на изображении нет персональных данных)."""
    moved_to: str | None = None
    note = ""
    with proj.transaction(p, rollback=False):
        source = next((s for s in p.sources.sources if s.id == sid), None)
        if source is None:
            raise CliError(f"источника {sid} нет в реестре")
        if source.path is None:
            raise CliError(f"{sid} — веб-источник: подтверждать нечего")
        pending = source.anonymized and not source.anonymization_confirmed
        image = source.kind == "изображение" and not source.no_personal_data_confirmed
        if not pending and not image:
            return {"source": sid, "changed": False, "moved": None, "note": f"у {sid} нечего подтверждать"}
        if pending:
            holder = pending_dir(p, sid)
            files = sorted(f for f in holder.glob("*") if f.is_file()) if holder.is_dir() else []
            for f in files:
                _move(f, private_dir(p.id, sid))
            if holder.is_dir():
                shutil.rmtree(holder, ignore_errors=True)
            moved_to = private_label(p.id, sid) if files else None
            if not files:
                note = "исходника в .tmp/ не нашлось (его уже убрали): подтверждена обезличенная копия"
            source.anonymization_confirmed = True
        if image:
            source.no_personal_data_confirmed = True
        p.save_sources()
        what = "замены персональных данных" if pending else "что на изображении нет персональных данных"
        p.add_event(
            p.event(
                kind="действие",
                name="strateg add --confirm",
                idempotency_hash=proj.key_hash("add confirm", sid),
                status="выполнено",
                duration_s=0,
                message=f"{sid}: человек подтвердил {what}"
                + (f"; исходник перенесён в {moved_to}" if moved_to else ""),
            )
        )
        p.save_state()
    return {"source": sid, "changed": True, "moved": moved_to, "note": note, "image": image and not pending}
