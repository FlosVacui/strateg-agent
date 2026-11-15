"""Проверка папки проекта целиком: `check_project(папка)` возвращает список ошибок по-русски.

Каждая ошибка называет файл и запись: `claims.yaml, запись У-3 (№3), поле quotes[0].text: ...`.
Форму записей проверяют модели из `strateg.models`; здесь — чтение файлов (UTF-8, LF), перевод
сообщений pydantic на русский и правила между записями и файлами: ссылки на существующие номера,
замена утверждений, циклы в основаниях и замене, снимки и их хеши, повторное вхождение цитаты в текст
снимка, повторные отметки выполнения. Файлы рамки, плана, пробелов, вариантов, обещаний, плана проверки
и решений необязательны; если они есть, их ссылки на утверждения, источники и друг на друга сверяются
здесь же, а имена версий в `history/` проверяются по виду `<файл>.v<N>.<расширение>`.

Абсолютные пути проверяют поля путей (тип `RelPath`); свободный текст — цитаты, пояснения,
сообщения — не просматривается.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from strateg.models import (
    LOCATOR_KINDS,
    Claim,
    ClaimsFile,
    CompletenessFile,
    CostRecord,
    DecisionsFile,
    FrameBody,
    FrameFile,
    GapsFile,
    JournalEvent,
    MetricsFile,
    OptionsFile,
    PlanFile,
    ProjectState,
    PromisesFile,
    Source,
    SourcesFile,
    VerificationFile,
    kind_of,
    latin_hint,
    norm,
    normalize_quote,
    quote_in_snapshot,
    show,
)

STATE_FILE = "state.yaml"
SOURCES_FILE = "sources.yaml"
CLAIMS_FILE = "claims.yaml"
JOURNAL_FILE = "journal.jsonl"
COST_FILE = "cost.jsonl"
FRAME_FILE = "frame.yaml"
PLAN_FILE = "plan.yaml"
GAPS_FILE = "gaps.yaml"
OPTIONS_FILE = "options.yaml"
PROMISES_FILE = "promises.yaml"
VERIFICATION_FILE = "verification.yaml"
DECISIONS_FILE = "decisions.yaml"
COMPLETENESS_FILE = "completeness.yaml"  # итог проверки полноты: пишет `strateg rubric record`
METRICS_FILE = "metrics.yaml"  # значения показателей: пишет `strateg metric add`; нет файла — значений нет
# Необязательные файлы выводов: пишет только CLI, у проекта на шаге 1 их ещё нет.
OUTPUT_FILES = (
    FRAME_FILE,
    PLAN_FILE,
    GAPS_FILE,
    OPTIONS_FILE,
    PROMISES_FILE,
    VERIFICATION_FILE,
    DECISIONS_FILE,
)
# Файлы, которые пишет CLI и хеши которых хранит state.yaml: файлы выводов и итог проверки полноты.
TRACKED_OUTPUTS = (*OUTPUT_FILES, COMPLETENESS_FILE)
PROJECT_FILES = (STATE_FILE, SOURCES_FILE, CLAIMS_FILE, JOURNAL_FILE, COST_FILE)
HISTORY_DIR = "history"
RELEASE_PREFIX = (
    "release-"  # снимок выпуска: history/release-<n>/ (файлы выхода и outputs/*.md на момент выпуска)
)
_RELEASE_DIR = re.compile(rf"^{RELEASE_PREFIX}([1-9][0-9]*)$")
# Часть сообщения об утверждении, которое отозвано или заменено, но стоит у рекомендации или обещания:
# команды замены и отзыва принимают такие ошибки, потому что места использования переписывает `write`.
STALE_REF_NOTE = "нельзя держать на отозванном или заменённом утверждении"
COMMS_STRATEGY = "outputs/comms-strategy.md"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def key_hash(*parts: object) -> str:
    """Ключ идемпотентности: хеш команды и входов (общий для команд и проверки журнала)."""
    payload = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Сообщения pydantic по-русски
# ---------------------------------------------------------------------------

_PATTERN_HINTS = {
    r"^И-[1-9][0-9]*$": "нужен номер источника вида И-1 (буква русская)",
    r"^У-[1-9][0-9]*$": "нужен номер утверждения вида У-1 (буква русская)",
    r"^ВП-[1-9][0-9]*$": "нужен номер вопроса плана вида ВП-1 (буквы русские)",
    r"^П-[1-9][0-9]*$": "нужен номер пробела вида П-1 (буква русская)",
    r"^Вар-[1-9][0-9]*$": "нужен номер варианта вида Вар-1 (буквы русские)",
    r"^О-[1-9][0-9]*$": "нужен номер обещания вида О-1 (буква русская)",
    r"^ПП-[1-9][0-9]*$": "нужен номер строки плана проверки вида ПП-1 (буквы русские)",
    r"^Реш-[1-9][0-9]*$": "нужен номер решения вида Реш-1 (буквы русские)",
    r"^[0-9a-f]{64}$": "нужен sha256: 64 символа 0-9 и a-f, строчными",
    r"^https?://[^\s]+$": "нужен адрес, начинающийся с http:// или https://",
    r"^[^\s]+ [0-9][^\s]*$": "нужны название и версия через пробел, например «trafilatura 2.0.0»",
    r"^[a-z0-9][a-z0-9_-]*$": "нужны латинские строчные буквы, цифры, «-» и «_»",
}
_ID_PATTERNS = frozenset(k for k in _PATTERN_HINTS if "-[1-9]" in k)
_QUOTE_HINT = "если это значение вроде no, 1.10 или дата, возьмите его в кавычки"


def _where(loc: Iterable[Any]) -> str:
    """Путь поля: `quotes[0].text`; ключ словаря — `files[«путь»] (ключ)`."""
    parts = list(loc)
    out, suffix = "", ""
    for i, part in enumerate(parts):
        if part == "[key]":
            suffix = " (ключ)"
        elif isinstance(part, int):
            out += f"[{part}]"
        elif i + 1 < len(parts) and parts[i + 1] == "[key]":
            out += f"[«{part}»]"
        else:
            out += ("." if out else "") + str(part)
    return out + suffix


def _allowed(expected: str) -> str:
    """Допустимые значения: «а», «б» или «в» — запятая внутри значения не путает со списком."""
    values = [f"«{v}»" for v in re.findall(r"'([^']*)'", expected)]
    if not values:
        return expected
    return values[0] if len(values) == 1 else ", ".join(values[:-1]) + " или " + values[-1]


def _explain(err: dict[str, Any]) -> list[str]:
    """Одна ошибка pydantic -> одно или несколько русских сообщений."""
    t, ctx, loc = err["type"], err.get("ctx") or {}, err["loc"]
    if t == "value_error":
        return [line for line in str(ctx.get("error", "некорректное значение")).split("\n") if line]
    if t == "missing":
        return ["нет обязательного поля"]
    if t == "extra_forbidden":
        return ["неизвестное поле: опечатка или поле не из этой версии схемы"]
    if t == "literal_error" and loc and loc[-1] == "schema_version":
        return [f"версия схемы «{show(err.get('input'))}» не поддерживается; ожидается {ctx.get('expected')}"]
    if t == "literal_error":
        hint = "" if isinstance(err.get("input"), str) else f"; {_QUOTE_HINT}"
        allowed = _allowed(str(ctx.get("expected")))
        return [f"недопустимое значение «{show(err.get('input'))}»; допустимо: {allowed}{hint}"]
    if t == "string_type":
        return [f"ожидается строка, а получено: {kind_of(err.get('input'))}; {_QUOTE_HINT}"]
    if t == "string_pattern_mismatch":
        pattern = ctx.get("pattern", "")
        hint = _PATTERN_HINTS.get(pattern, f"нужен шаблон {pattern}")
        latin = latin_hint(err.get("input")) if pattern in _ID_PATTERNS else ""
        return [f"значение «{show(err.get('input'))}» не подходит: {hint}{latin}"]
    if t == "string_too_short":
        return ["строка пустая"]
    if t in ("too_short", "too_long"):
        have = len(err["input"]) if isinstance(err.get("input"), list) else 0
        bound = (
            f"не меньше {ctx.get('min_length')}" if t == "too_short" else f"не больше {ctx.get('max_length')}"
        )
        return [f"в списке записей: {have}, нужно {bound}"]
    if t in ("bool_type", "bool_parsing"):
        return [f"ожидается логическое значение true или false, а получено: {kind_of(err.get('input'))}"]
    if t in ("int_type", "int_parsing", "int_from_float"):
        return [f"ожидается целое число, а получено: {kind_of(err.get('input'))}"]
    if t == "finite_number":
        return ["ожидается конечное число: Infinity и NaN недопустимы"]
    if t in ("float_type", "float_parsing"):
        return [f"ожидается число, а получено: {kind_of(err.get('input'))}"]
    if t in ("greater_than_equal", "greater_than", "less_than_equal", "less_than"):
        return [f"число {show(err.get('input'))} вне допустимых границ"]
    if t == "timezone_aware":
        return ["время без часового пояса: добавьте пояс, например 2026-11-06T10:00:00+03:00"]
    if t.startswith("datetime") or t.startswith("date_"):
        return ["ожидаются дата и время в формате ISO 8601 с поясом, например 2026-11-06T10:00:00+03:00"]
    if t in ("list_type", "set_type", "tuple_type"):
        return ["ожидается список"]
    if t in ("dict_type", "model_type", "model_attributes_type"):
        return ["ожидается набор полей «ключ: значение»"]
    return ["некорректное значение"]


_RECORD_ID = re.compile(r"^(?:[УИП]|ВП|ПП|Вар|О|Реш)-[1-9][0-9]*$")


def _record_label(file: str, sections: tuple[str, ...], section: str, raw: Any, index: int) -> str:
    """Подпись записи: `файл, запись У-3 (№3)`; у записи второго раздела файла — с названием раздела."""
    rec = None
    if isinstance(raw, dict) and isinstance(raw.get(section), list) and index < len(raw[section]):
        rec = raw[section][index]
    ident = rec.get("id") if isinstance(rec, dict) else None
    head = file if section == sections[0] else f"{file}, {section}"
    if isinstance(ident, str) and _RECORD_ID.match(ident):
        return f"{head}, запись {ident} (№{index + 1})"
    return f"{head}, запись №{index + 1}"


def _format_errors(file: str, sections: tuple[str, ...], raw: Any, exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = list(err["loc"])
        label = file
        if len(loc) >= 2 and loc[0] in sections and isinstance(loc[1], int):
            label = _record_label(file, sections, str(loc[0]), raw, loc[1])
            loc = loc[2:]
        field = f", поле {_where(loc)}" if loc else ""
        out.extend(f"{label}{field}: {m}" for m in _explain(err))
    return out


# ---------------------------------------------------------------------------
# Чтение файлов
# ---------------------------------------------------------------------------


def _read_text(root: Path, rel: str, errors: list[str]) -> str | None:
    """Читает файл как UTF-8 без BOM и с окончаниями LF; иначе добавляет ошибку."""
    path = root / rel
    if not path.is_file():
        errors.append(f"{rel}: файл не найден")
        return None
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{rel}: файл с BOM, нужен UTF-8 без BOM")
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        errors.append(f"{rel}: файл не в кодировке UTF-8 (байт {e.start})")
        return None
    if "\r" in text:
        errors.append(f"{rel}: окончания строк CRLF, нужны LF")
        return None
    return text


class _DuplicateKeyError(yaml.YAMLError):
    def __init__(self, key: Any, line: int) -> None:
        super().__init__(key, line)
        self.key = key
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    """`yaml.safe_load` молча берёт последний из повторяющихся ключей; здесь повтор — ошибка."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    if key in seen:
                        raise _DuplicateKeyError(key, key_node.start_mark.line + 1)
                    seen.add(key)
                except TypeError:
                    break  # ключ нельзя хешировать: ошибку даст сам разбор
        return super().construct_mapping(node, deep)


def _load_yaml(root: Path, rel: str, errors: list[str]) -> Any | None:
    text = _read_text(root, rel, errors)
    if text is None:
        return None
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - загрузчик на базе SafeLoader
    except _DuplicateKeyError as e:
        errors.append(f"{rel}: повторяющийся ключ {show(e.key)} в строке {e.line}; второй перекрыл бы первый")
        return None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" в строке {mark.line + 1}" if mark else ""
        errors.append(f"{rel}: не читается как YAML{where}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{rel}: файл пуст или в корне не набор «ключ: значение»")
        return None
    return data


def _validate(
    rel: str, sections: str | tuple[str, ...] | None, model: type[BaseModel], raw: Any, errors: list[str]
) -> BaseModel | None:
    try:
        return model.model_validate(raw, by_name=False)
    except ValidationError as exc:
        names = (sections,) if isinstance(sections, str) else sections or ()
        errors.extend(_format_errors(rel, names, raw, exc))
        return None


def _load_jsonl(root: Path, rel: str, model: type[BaseModel], errors: list[str]) -> list[Any] | None:
    """Записи JSONL; при ошибке в строке остальные всё равно проверяются, результат — None."""
    text = _read_text(root, rel, errors)
    if text is None:
        return None
    records: list[Any] = []
    ok = True
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # завершающий перевод строки
    elif lines:
        errors.append(f"{rel}: нет завершающего перевода строки; следующая запись склеилась бы с последней")
        ok = False
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            errors.append(f"{rel}, строка {n}: пустая строка")
            ok = False
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{rel}, строка {n}: не JSON (позиция {e.colno})")
            ok = False
            continue
        try:
            records.append(model.model_validate(raw, by_name=False))
        except ValidationError as exc:
            ok = False
            errors.extend(m.replace(rel, f"{rel}, строка {n}", 1) for m in _format_errors(rel, (), raw, exc))
    return records if ok else None


# ---------------------------------------------------------------------------
# Правила между записями и файлами
# ---------------------------------------------------------------------------


def _duplicates(ids: list[str]) -> set[str]:
    return {i for i in ids if ids.count(i) > 1}


def _check_sources_files(root: Path, sources: SourcesFile, errors: list[str]) -> None:
    for n, s in enumerate(sources.sources, start=1):
        label = f"{SOURCES_FILE}, запись {s.id} (№{n})"
        if s.closed_markers and not s.closed_confirmed:
            errors.append(
                f"{label}: найдены признаки закрытых данных, но человек не подтвердил, "
                "что материал общедоступный"
            )
        if s.anonymized and not s.anonymization_confirmed and s.path is not None:
            errors.append(
                f"{label}: в материале найдены персональные данные ({', '.join(s.anonymized)}), "
                "человек не подтвердил замены: `strateg add --confirm " + s.id + "`"
            )
        if s.kind == "изображение" and not s.no_personal_data_confirmed:
            errors.append(
                f"{label}: человек не подтвердил, что на изображении нет персональных данных: "
                "`strateg add --confirm " + s.id + "`"
            )
        if s.path is not None and not (root / s.path).is_file():
            errors.append(f"{label}, поле path: файл {s.path} не найден")
        if s.text_path is None:
            continue
        text = root / s.text_path
        if not text.is_file():
            errors.append(f"{label}, поле text_path: файл {s.text_path} не найден")
            continue
        sub: list[str] = []
        if _read_text(root, s.text_path, sub) is None:
            errors.extend(f"{label}: {m}" for m in sub)
        if s.text_sha256 and sha256_file(text) != s.text_sha256:
            errors.append(
                f"{label}, поле text_sha256: хеш не совпадает с файлом {s.text_path}; снимок изменён"
            )


_SNAPSHOT_NAME = re.compile(r"^i-[1-9][0-9]*\.txt$")


def _check_unreferenced_files(root: Path, sources: SourcesFile, errors: list[str]) -> None:
    """Снимки `sources/i-<n>.txt`, на которые не ссылается ни одна запись реестра.

    Так видна потеря записи: `fetch` записал снимок, а запись об источнике затёрта соседней командой.
    Считаются только имена снимков; мусор ОС (`Thumbs.db`, `desktop.ini`) и временные файлы записи не в счёт.
    В `inputs/` потерь не ищется: там лежат файлы человека, а запись `add` не теряется (блокировка папки).
    """
    referenced = {s.text_path for s in sources.sources if s.text_path}
    snapshots = root / "sources"
    if not snapshots.is_dir():
        return
    for p in sorted(snapshots.iterdir()):
        rel = f"sources/{p.name}"
        if p.is_file() and _SNAPSHOT_NAME.match(p.name) and rel not in referenced:
            errors.append(
                f"{rel}: снимок, на который не ссылается ни одна запись {SOURCES_FILE} "
                "(запись об источнике потеряна, например параллельной командой). "
                "Загрузите адрес заново: `strateg fetch <адрес>`; лишний файл уберите вручную"
            )


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Первый цикл в графе ссылок без рекурсии: список номеров по кругу или None."""
    state: dict[str, int] = {}  # 1 — на текущем пути, 2 — разобран
    for start in edges:
        if start in state:
            continue
        path = [start]
        iters = [iter(edges[start])]
        state[start] = 1
        while path:
            nxt = next(iters[-1], None)
            if nxt is None:
                state[path.pop()] = 2
                iters.pop()
                continue
            if nxt not in edges:
                continue
            if state.get(nxt) == 1:
                return path[path.index(nxt) :] + [nxt]
            if nxt not in state:
                state[nxt] = 1
                path.append(nxt)
                iters.append(iter(edges[nxt]))
    return None


def _snapshot_text(root: Path, src: Source, cache: dict[str, str | None]) -> str | None:
    """Текст снимка источника; None, если файла нет, он изменён или не читается (ошибки уже названы)."""
    if src.id not in cache:
        text = None
        if src.text_path and src.text_sha256:
            path = root / src.text_path
            if path.is_file() and sha256_file(path) == src.text_sha256:
                try:
                    text = path.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    text = None
        cache[src.id] = text
    return cache[src.id]


def _check_claims(root: Path, sources: SourcesFile, claims: ClaimsFile, errors: list[str]) -> None:
    snapshots: dict[str, str | None] = {}
    by_src = {s.id: s for s in sources.sources}
    by_id = {c.id: c for c in claims.claims}
    for dup in sorted(_duplicates([c.id for c in claims.claims])):
        errors.append(f"{CLAIMS_FILE}: номер {dup} встречается больше одного раза")

    for n, c in enumerate(claims.claims, start=1):
        label = f"{CLAIMS_FILE}, запись {c.id} (№{n})"

        def err(msg: str, label: str = label) -> None:
            errors.append(f"{label}{', ' if msg.startswith('поле ') else ': '}{msg}")

        for field, refs in (
            ("basis", c.basis),
            ("replaces", [c.replaces] if c.replaces else []),
            ("replaced_by", [c.replaced_by] if c.replaced_by else []),
            ("dispute", [c.dispute.claim] if c.dispute else []),
        ):
            for ref in refs:
                if ref not in by_id:
                    err(f"поле {field}: ссылка на несуществующее утверждение {ref}")

        # Замена: те же предмет, период и регион, обратная ссылка.
        if c.replaced_by and c.replaced_by in by_id:
            new = by_id[c.replaced_by]
            same = (norm(c.subject), norm(c.period or ""), norm(c.region or "")) == (
                norm(new.subject),
                norm(new.period or ""),
                norm(new.region or ""),
            )
            if not same:
                err(
                    f"«заменено» на {new.id}, но предмет, период или регион у них разные; "
                    "оба утверждения должны действовать, расхождение идёт в картину"
                )
            if new.replaces != c.id:
                err(f"у {new.id} нет обратной ссылки replaces: {c.id}")
        if c.replaces and c.replaces in by_id:
            old = by_id[c.replaces]
            if old.replaced_by != c.id:
                err(f"у {old.id} нет ссылки replaced_by: {c.id}")
            if old.status != "заменено":
                err(f"заменяет {old.id}, но у него статус «{old.status}», а не «заменено»")

        quote_sources = []
        for qi, q in enumerate(c.quotes):
            where = f"поле quotes[{qi}]"
            src = by_src.get(q.source)
            if src is None:
                err(f"{where}: ссылка на несуществующий источник {q.source}")
                continue
            quote_sources.append(src)
            if src.text_sha256 is None:
                err(f"{where}: у источника {src.id} нет снимка текста (доступ: «{src.access}»)")
                continue
            if q.check is not None and q.check.snapshot_sha256 != src.text_sha256:
                err(
                    f"{where}: цитата проверена по другому снимку, чем сейчас у {src.id}; проверьте её заново"
                )
            if src.kind in LOCATOR_KINDS and q.locator is None:
                err(f"{where}: у цитаты из источника вида «{src.kind}» нет места (locator)")
            # Запись проверки перепроверяется по тексту снимка.
            if q.check is not None and q.check.snapshot_sha256 == src.text_sha256:
                text = _snapshot_text(root, src, snapshots)
                if text is not None and not quote_in_snapshot(q.check.method, q.text, text):
                    err(
                        f"{where}: цитата не найдена в снимке {src.id} ({src.text_path}) способом "
                        f"«{q.check.method}»; запись проверки текстом снимка не подтверждается"
                    )

        if c.type == "факт":
            checked = [by_src[q.source] for q in c.quotes if q.check is not None and q.source in by_src]
            if checked and all(s.kind == "синтетика" for s in checked):
                err(
                    "факт из синтетики: цитата только из источника вида «синтетика», фактом она не становится"
                )
            for src in {s.id: s for s in quote_sources if s.fetch and s.fetch.region}.values():
                if norm(c.region or "") != norm(src.fetch.region or ""):
                    err(
                        f"у источника {src.id} регион «{src.fetch.region}», а у факта "
                        f"«{c.region or 'не указан'}»: регионы должны совпадать"
                    )
        if any(s.suspicious for s in quote_sources) and not c.suspicious:
            err("источник помечен «подозрительный», а утверждение нет (suspicious)")

    cycle = _find_cycle({c.id: list(c.basis) for c in claims.claims})
    if cycle:
        errors.append(f"{CLAIMS_FILE}: цикл в основаниях: {' -> '.join(cycle)}")
    cycle = _find_cycle({c.id: [c.replaced_by] if c.replaced_by else [] for c in claims.claims})
    if cycle:
        errors.append(f"{CLAIMS_FILE}: цикл замены: {' -> '.join(cycle)}")


def _intact(root: Path, s: Source) -> bool:
    """Источник получен, а его снимок или материал на месте и совпадает с записанным хешем."""
    if s.access != "получен" or (s.text_path is None and s.path is None):
        return False
    for rel in (s.text_path, s.path):
        if rel is not None and not (root / rel).is_file():
            return False
    return s.text_path is None or not s.text_sha256 or sha256_file(root / s.text_path) == s.text_sha256


def _one_source(root: Path, group: list[Source], refreshed: set[str]) -> bool:
    """Источники одного адреса — по сути один: звенья одной цепочки перепроверок (`refreshes`).

    Новый номер после `fetch --refresh` с изменившимся текстом — законное второе звено, а не двойной источник.
    Нужен один последний источник (на него никто не ссылается через `refreshes`), он цел, а остальные звенья
    связаны с ним цепочкой. Два несвязанных источника на адрес — двойной источник.
    """
    ids = {s.id for s in group}
    tips = [s for s in group if s.id not in refreshed]
    if len(tips) != 1 or not _intact(root, tips[0]):
        return False
    by_id = {s.id: s for s in group}
    reached = {tips[0].id}
    link = tips[0]
    while link.refreshes in ids and link.refreshes not in reached:
        reached.add(link.refreshes)
        link = by_id[link.refreshes]
    return reached == ids


def _harmless_repeats(root: Path, sources: SourcesFile | None) -> set[str]:
    """Ключи, у которых повторное «выполнено» в журнале безвредно.

    Повтор остаётся от гонки параллельных команд (или от повторной загрузки после неё): команда записала
    «выполнено» второй раз с тем же ключом. Он безвреден, когда в реестре один источник на этот адрес
    (`fetch`; цепочка перепроверок `refreshes` — один источник) или на этот файл (`add`) и его снимок цел:
    двойного источника и потерянной записи за ним нет.
    Журнал только дописывается, поэтому строки повтора остаются, а проверка их принимает.
    """
    if sources is None:
        return set()
    by_address: dict[tuple[str, str | None], list[Source]] = {}
    by_material: dict[str, list[Source]] = {}
    for s in sources.sources:
        if s.url is not None:
            region = s.fetch.region if s.fetch else None
            for url in {s.url, *(s.other_urls or [])}:
                by_address.setdefault((url, region), []).append(s)
        if s.material_sha256:
            by_material.setdefault(s.material_sha256, []).append(s)
    refreshed = {s.refreshes for s in sources.sources if s.refreshes}
    keys = {
        key_hash("fetch", url, region)
        for (url, region), group in by_address.items()
        if _one_source(root, group, refreshed)
    }
    keys |= {
        key_hash("add", sha)
        for sha, group in by_material.items()
        if len(group) == 1 and _intact(root, group[0])
    }
    return keys


def _check_events(
    root: Path,
    events: list[JournalEvent],
    sources: SourcesFile | None,
    errors: list[str],
    frame_present: bool = False,
) -> None:
    done: dict[str, int] = {}
    ids = {s.id for s in sources.sources} if sources else None
    harmless = _harmless_repeats(root, sources)
    for n, e in enumerate(events, start=1):
        h = e.idempotency_hash
        if e.kind == "действие" and e.status == "выполнено" and h:
            if h in done and h not in harmless:
                errors.append(
                    f"{JOURNAL_FILE}, строка {n}: действие уже выполнено (строка {done[h]}), "
                    "повторная отметка выполнения с тем же ключом"
                )
            done.setdefault(h, n)
        if e.source and ids is not None and e.source not in ids:
            errors.append(f"{JOURNAL_FILE}, строка {n}: ссылка на несуществующий источник {e.source}")
        g = e.gate
        if g is None:
            continue
        if g.frame is not None:
            _frame_sources(errors, f"{JOURNAL_FILE}, строка {n}", "gate.frame.", g.frame, ids)
        elif frame_present and g.gate == "В1" and g.answered_at is not None:
            errors.append(
                f"{JOURNAL_FILE}, строка {n}: у отвеченного В1 нет поля frame, а {FRAME_FILE} есть; "
                "событие хранит все поля рамки и откуда каждое"
            )


def _check_costs(records: list[CostRecord], errors: list[str]) -> None:
    seen: dict[tuple[str, str], int] = {}
    for n, r in enumerate(records, start=1):
        if r.entry == "по сообщению" and r.message_id:
            key = (r.session_id, r.message_id)
            if key in seen:
                errors.append(
                    f"{COST_FILE}, строка {n}: сообщение {r.message_id} уже учтено (строка {seen[key]}), "
                    "повтор удвоил бы расход"
                )
            seen.setdefault(key, n)


# ---------------------------------------------------------------------------
# Файлы выводов: рамка, план, пробелы, варианты, обещания, план проверки, решения
# ---------------------------------------------------------------------------

_FRAME_FIELDS = (
    "decision",
    "deadline",
    "research_question",
    "recipient",
    "constraints",
    "data",
    "region",
    "success_criteria",
    "out_of_scope",
)
_PLAN_REF = re.compile(r"ВП-[1-9][0-9]*")
_GAP_REF = re.compile(r"П-[1-9][0-9]*")
# «Почти номера»: латинские буквы вместо русских, строчные, ведущий ноль, несколько номеров в одном поле.
_PLAN_LIKE_ONE = r"[ВB][ПP]\s*[-–—]\s*[0-9]+"
_PLAN_LIKE = re.compile(rf"{_PLAN_LIKE_ONE}(?:(?:\s*[,;/]\s*|\s+и\s+){_PLAN_LIKE_ONE})*", re.IGNORECASE)
_GAP_LIKE = re.compile(r"[ПP]\s*[-–—]\s*[0-9]+", re.IGNORECASE)
_PROMISE_MARK = re.compile(r"\[([OО])-([0-9]+)\]")


class _Registry:
    """Номера одного файла, на которые ссылаются другие записи."""

    def __init__(self, file: str, ids: set[str] | None, exists: bool) -> None:
        self.file = file
        self.ids = ids  # None: файл есть, но не разобран — его ошибки уже названы
        self.exists = exists

    def check(self, errors: list[str], where: str, field: str, ref: str) -> None:
        if not self.exists:
            errors.append(f"{where}, поле {field}: ссылка на {ref}, но файла {self.file} нет")
        elif self.ids is not None and ref not in self.ids:
            errors.append(f"{where}, поле {field}: ссылка на {ref}, такого номера нет в {self.file}")


class _Claims:
    """Ссылки на утверждения: номер существует, тип и статус подходят месту ссылки."""

    def __init__(self, claims: ClaimsFile | None, errors: list[str]) -> None:
        self.by_id: dict[str, Claim] | None = {c.id: c for c in claims.claims} if claims else None
        self.errors = errors

    def type_of(self, ref: str) -> str | None:
        c = self.by_id.get(ref) if self.by_id is not None else None
        return c.type if c else None

    def only_self_reported(self, refs: list[str]) -> list[str] | None:
        """Факты среди `refs`, если они все — заявления издателя о себе; иначе (есть независимый или
        вовсе нет фактов) None.

        Независимый факт — тот, у которого хотя бы одна проверенная цитата из источника не «о себе»
        (`self_reported: false`).
        """
        if self.by_id is None:
            return None
        facts = [self.by_id[r] for r in refs if r in self.by_id and self.by_id[r].type == "факт"]
        if not facts:
            return None
        if any(q.self_reported is False for c in facts for q in c.quotes if q.check is not None):
            return None
        return [c.id for c in facts]

    def ref(
        self,
        where: str,
        field: str,
        ref: str,
        *,
        type_: str | tuple[str, ...] | None = None,
        live_for: str | None = None,
        synthetic_note: str | None = None,
    ) -> None:
        if self.by_id is None:
            return
        c = self.by_id.get(ref)
        if c is None:
            self.errors.append(f"{where}, поле {field}: ссылка на несуществующее утверждение {ref}")
            return
        if c.type == "синтетика" and synthetic_note:
            self.errors.append(f"{where}, поле {field}: {ref} — синтетика: {synthetic_note}")
        elif type_ is not None and c.type not in ((type_,) if isinstance(type_, str) else type_):
            wanted = "» или «".join((type_,) if isinstance(type_, str) else type_)
            self.errors.append(f"{where}, поле {field}: {ref} имеет тип «{c.type}», а здесь нужен «{wanted}»")
        if live_for and c.status != "действует":
            self.errors.append(
                f"{where}, поле {field}: {ref} имеет статус «{c.status}»; {live_for} {STALE_REF_NOTE}, "
                "возьмите действующее"
            )


def _frame_sources(
    errors: list[str], where: str, prefix: str, body: Any, source_ids: set[str] | None
) -> None:
    if source_ids is None:
        return
    for name in _FRAME_FIELDS:
        src = getattr(body, name).source
        if src is not None and src not in source_ids:
            errors.append(f"{where}, поле {prefix}{name}.source: ссылка на несуществующий источник {src}")


def _load_optional(
    root: Path, rel: str, model: type[BaseModel], sections: tuple[str, ...], errors: list[str]
) -> tuple[bool, Any]:
    """(файл есть, разобранная модель или None, если файл не разобран)."""
    if not (root / rel).is_file():
        return False, None
    raw = _load_yaml(root, rel, errors)
    if raw is None:
        return True, None
    return True, _validate(rel, sections, model, raw, errors)


def _dup_ids(file: str, ids: list[str], errors: list[str]) -> None:
    for dup in sorted(_duplicates(ids)):
        errors.append(f"{file}: номер {dup} встречается больше одного раза")


def _own(file: str, ids: list[str]) -> _Registry:
    return _Registry(file, set(ids), True)


def _check_outputs(
    root: Path, sources: SourcesFile | None, claims: ClaimsFile | None, errors: list[str]
) -> tuple[bool, FrameFile | None]:
    """Читает необязательные файлы выводов и сверяет их ссылки.

    Возвращает: есть ли `frame.yaml` и его разобранная модель (None, если файл не разобран).
    """
    cl = _Claims(claims, errors)
    source_ids = {s.id for s in sources.sources} if sources else None
    frame_ok, frame = _load_optional(root, FRAME_FILE, FrameFile, (), errors)
    plan_ok, plan = _load_optional(root, PLAN_FILE, PlanFile, ("questions",), errors)
    gaps_ok, gaps = _load_optional(root, GAPS_FILE, GapsFile, ("gaps", "metric_coverage"), errors)
    opts_ok, opts = _load_optional(root, OPTIONS_FILE, OptionsFile, ("options",), errors)
    prom_ok, prom = _load_optional(root, PROMISES_FILE, PromisesFile, ("promises",), errors)
    ver_ok, ver = _load_optional(
        root, VERIFICATION_FILE, VerificationFile, ("rows", "premortem", "undesired_effects"), errors
    )
    _, dec = _load_optional(root, DECISIONS_FILE, DecisionsFile, ("decisions",), errors)
    _, metrics = _load_optional(root, METRICS_FILE, MetricsFile, ("values",), errors)
    _, comp = _load_optional(root, COMPLETENESS_FILE, CompletenessFile, ("part_a", "part_b"), errors)
    if comp is not None:
        _check_completeness(comp, errors)

    gap_reg = _Registry(GAPS_FILE, {g.id for g in gaps.gaps} if gaps else None, gaps_ok)
    opt_reg = _Registry(OPTIONS_FILE, {o.id for o in opts.options} if opts else None, opts_ok)
    plan_reg = _Registry(PLAN_FILE, {q.id for q in plan.questions} if plan else None, plan_ok)
    ver_reg = _Registry(VERIFICATION_FILE, {r.id for r in ver.rows} if ver else None, ver_ok)

    if frame is not None:
        _frame_sources(errors, FRAME_FILE, "", frame, source_ids)
    if plan is not None:
        _check_plan(plan, gap_reg, errors)
    if gaps is not None:
        _check_gaps(gaps, cl, opt_reg, errors)
    if opts is not None:
        _check_options(opts, cl, errors)
    if prom is not None:
        _check_promises(prom, cl, errors)
    if ver is not None:
        _check_verification(ver, cl, gap_reg, opts, errors)
    if dec is not None:
        _check_decisions(dec, cl, opt_reg, ver_reg, ver, errors)
    if metrics is not None:
        _check_metrics(metrics, ver_reg, errors)
    if plan is not None and claims is not None:
        _check_plan_questions(claims, plan_reg, errors)
    _check_text_refs(
        (
            (FRAME_FILE, (), frame),
            (PLAN_FILE, ("questions",), plan),
            (GAPS_FILE, ("gaps", "metric_coverage"), gaps),
            (OPTIONS_FILE, ("options",), opts),
            (PROMISES_FILE, ("promises",), prom),
            (VERIFICATION_FILE, ("rows", "premortem", "undesired_effects"), ver),
            (DECISIONS_FILE, ("decisions",), dec),
        ),
        cl,
        gap_reg,
        opt_reg,
        errors,
    )
    _check_promise_marks(root, prom_ok, prom, errors)
    _check_history(root, frame, errors)
    return frame_ok, frame


def _check_completeness(comp: CompletenessFile, errors: list[str]) -> None:
    """Номера частей А и Б есть в рубрике, ответы на все вопросы части Б есть, итог равен выводу по
    `pass_rule`. Рубрика не читается (нет файла) — сверять не с чем, ошибок нет."""
    from strateg import rubric  # импорт здесь: rubric импортирует этот модуль

    try:
        rb = rubric.load_rubric()
    except rubric.CliError:
        return
    known_a = {a.id for a in rb.part_a}
    known_b = {b.id for b in rb.part_b}
    for r in comp.part_a:
        if r.id not in known_a:
            errors.append(f"{COMPLETENESS_FILE}, поле part_a: признака {r.id} нет в рубрике")
    for r in comp.part_b:
        if r.id not in known_b:
            errors.append(f"{COMPLETENESS_FILE}, поле part_b: вопроса {r.id} нет в рубрике")
    given = {r.id for r in comp.part_b}
    for q in rb.part_b:
        if q.id not in given:
            errors.append(f"{COMPLETENESS_FILE}, поле part_b: нет ответа на вопрос {q.id}")
    if given != known_b or any(r.id not in known_a for r in comp.part_a):
        return  # лишние и недостающие номера названы выше
    want = rubric.verdict(rb, comp.part_a, comp.part_b)
    if comp.result != want:
        errors.append(f"{COMPLETENESS_FILE}, поле result: «{comp.result}», а по pass_rule рубрики — «{want}»")


# Номера в свободном тексте: «закроем П-9», «как в Вар-2», «см. У-14». Не путать с «ВП-2» и «ПП-3»:
# перед буквой не должно быть слова или дефиса.
_TEXT_REF = re.compile(r"(?<![\w-])(Вар|П|У)-([1-9][0-9]*)(?!\d)")
_ONLY_REF = re.compile(r"(?:Вар|ВП|ПП|Реш|[ПУО])-[1-9][0-9]*")


def _strings(value: Any, path: tuple[Any, ...]) -> Iterable[tuple[tuple[Any, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, (*path, i))


def _check_text_refs(
    files: Iterable[tuple[str, tuple[str, ...], BaseModel | None]],
    cl: _Claims,
    gap_reg: _Registry,
    opt_reg: _Registry,
    errors: list[str],
) -> None:
    """Номера `П-n`, `У-n` и `Вар-n` в текстовых полях файлов выхода сверяются с реестрами, как ссылки
    в полях-ссылках. Поле, в котором стоит только номер, — ссылка: её сверяют правила своего поля."""
    for file, sections, model in files:
        if model is None:
            continue
        raw = model.model_dump(mode="json", by_alias=True, exclude_none=True)
        for path, text in _strings(raw, ()):
            if _ONLY_REF.fullmatch(text.strip()):
                continue
            if len(path) >= 2 and path[0] in sections and isinstance(path[1], int):
                where = _record_label(file, sections, str(path[0]), raw, path[1])
                field = _where(path[2:])
            else:
                where, field = file, _where(path)
            for letter, number in dict.fromkeys(_TEXT_REF.findall(text)):
                ref = f"{letter}-{number}"
                head = f"{where}, поле {field}: в тексте названо {ref}"
                if letter == "У":
                    if cl.by_id is not None and ref not in cl.by_id:
                        errors.append(f"{head}, такого утверждения нет в реестре")
                    continue
                reg = gap_reg if letter == "П" else opt_reg
                if not reg.exists:
                    errors.append(f"{head}, но файла {reg.file} нет")
                elif reg.ids is not None and ref not in reg.ids:
                    errors.append(f"{head}, такого номера нет в {reg.file}")


def _check_plan(plan: PlanFile, gap_reg: _Registry, errors: list[str]) -> None:
    _dup_ids(PLAN_FILE, [q.id for q in plan.questions], errors)
    for n, q in enumerate(plan.questions, start=1):
        if q.gap:
            gap_reg.check(errors, f"{PLAN_FILE}, запись {q.id} (№{n})", "gap", q.gap)


_SYNTHETIC_PROOF = "ответ модельного респондента ничего не доказывает, опорой он быть не может"


def _check_gaps(gaps: GapsFile, cl: _Claims, opt_reg: _Registry, errors: list[str]) -> None:
    _dup_ids(GAPS_FILE, [g.id for g in gaps.gaps], errors)
    own = _own(GAPS_FILE, [g.id for g in gaps.gaps])
    kinds = {g.id: g.metric_kind for g in gaps.gaps}
    for n, g in enumerate(gaps.gaps, start=1):
        where = f"{GAPS_FILE}, запись {g.id} (№{n})"
        for opt in g.affects_options:
            opt_reg.check(errors, where, "affects_options", opt)
        if g.assumption:
            cl.ref(where, "assumption", g.assumption, type_="допущение")
        if g.hypothesis:
            cl.ref(where, "hypothesis", g.hypothesis, type_="гипотеза")
        for ref in g.closed_by:
            cl.ref(where, "closed_by", ref, synthetic_note=_SYNTHETIC_PROOF)
    for n, m in enumerate(gaps.metric_coverage, start=1):
        where = f"{GAPS_FILE}, metric_coverage, запись №{n}"
        for ref in m.claims or []:
            cl.ref(where, "claims", ref, type_=("факт", "прогноз"), synthetic_note=_SYNTHETIC_PROOF)
        if m.gap:
            own.check(errors, where, "gap", m.gap)
            if kinds.get(m.gap) not in (None, m.kind):
                errors.append(
                    f"{where}, поле gap: пробел {m.gap} относится к показателю «{kinds[m.gap]}», "
                    f"а запись — к «{m.kind}»"
                )


def _check_options(opts: OptionsFile, cl: _Claims, errors: list[str]) -> None:
    _dup_ids(OPTIONS_FILE, [o.id for o in opts.options], errors)
    own = _own(OPTIONS_FILE, [o.id for o in opts.options])
    for n, o in enumerate(opts.options, start=1):
        where = f"{OPTIONS_FILE}, запись {o.id} (№{n})"
        own.check(errors, where, "differs.from", o.differs.from_)
        for i, obj in enumerate(o.objections):
            if obj.basis != "гипотеза":
                cl.ref(
                    where,
                    f"objections[{i}].basis",
                    obj.basis,
                    type_=("факт", "гипотеза"),
                    synthetic_note=_SYNTHETIC_PROOF,
                )
        for name in ("effect", "first_signal"):
            cell = getattr(o.comparison, name)
            if cell.forecast:
                cl.ref(where, f"comparison.{name}.forecast", cell.forecast, type_="прогноз")
    rec = opts.recommendation
    where = f"{OPTIONS_FILE}, recommendation"
    own.check(errors, where, "option", rec.option)
    for ref in rec.claims:
        cl.ref(where, "claims", ref, live_for="рекомендацию", synthetic_note=_SYNTHETIC_PROOF)
    known = [cl.type_of(ref) for ref in rec.claims]
    if None not in known and "факт" not in known:
        errors.append(
            f"{where}, поле claims: среди оснований нет ни одного факта; рекомендация не держится "
            "только на прогнозах, гипотезах и допущениях"
        )
    only_self = cl.only_self_reported(rec.claims)
    if only_self:
        errors.append(
            f"{where}, поле claims: рекомендация держится только на заявлении компании о себе: факты "
            f"{', '.join(only_self)} взяты из источников, где издатель пишет о себе или своём продукте "
            "(self_reported: true); добавьте факт из независимого источника (отчёт регулятора, "
            "исследование, отзывы пользователей) или честно назовите это пробелом"
        )
    if opts.chosen:
        own.check(errors, OPTIONS_FILE, "chosen", opts.chosen)


def _check_promises(prom: PromisesFile, cl: _Claims, errors: list[str]) -> None:
    _dup_ids(PROMISES_FILE, [p.id for p in prom.promises], errors)
    for n, p in enumerate(prom.promises, start=1):
        where = f"{PROMISES_FILE}, запись {p.id} (№{n})"
        for ref in p.claims:
            cl.ref(where, "claims", ref, type_="факт", live_for="обещание")


def _threshold_key(text: str) -> str:
    """Порог для сравнения: нормализация цитат, пробелы вокруг знаков не важны («20%» и «20 %»)."""
    return re.sub(r"\s*([^\w\s])\s*", lambda m: m.group(1), normalize_quote(text)).strip(" .,;:!")


def _same_threshold(a: str, b: str) -> bool:
    return _threshold_key(a) == _threshold_key(b)


def _check_verification(
    ver: VerificationFile,
    cl: _Claims,
    gap_reg: _Registry,
    opts: OptionsFile | None,
    errors: list[str],
) -> None:
    _dup_ids(VERIFICATION_FILE, [r.id for r in ver.rows], errors)
    own = _own(VERIFICATION_FILE, [r.id for r in ver.rows])
    kinds = {r.id: r.kind for r in ver.rows}
    synthetic = "ответ модельного респондента ничего не измеряет, показатель на нём не строится"
    for n, r in enumerate(ver.rows, start=1):
        where = f"{VERIFICATION_FILE}, запись {r.id} (№{n})"
        if r.hypothesis:
            cl.ref(where, "hypothesis", r.hypothesis, type_="гипотеза", synthetic_note=synthetic)
        base = r.baseline.strip()
        if _GAP_REF.fullmatch(base):
            gap_reg.check(errors, where, "baseline", base)
        elif _GAP_LIKE.fullmatch(base):
            errors.append(
                f"{where}, поле baseline: значение «{base}» похоже на номер пробела, но не подходит: "
                f"нужен номер вида П-1 (буква русская заглавная, без нуля впереди){latin_hint(base)}; "
                "если нужен текст, добавьте слова к номеру"
            )
        if r.from_change_mind is not None and opts is not None:
            mind = opts.recommendation.change_mind
            if r.from_change_mind > len(mind):
                errors.append(
                    f"{where}, поле from_change_mind: записи {r.from_change_mind} нет в "
                    f"recommendation.change_mind из {OPTIONS_FILE} (там записей: {len(mind)})"
                )
            elif not _same_threshold(r.change_mind.threshold, mind[r.from_change_mind - 1].threshold):
                errors.append(
                    f"{where}, поле from_change_mind: порог строки «{r.change_mind.threshold}» не совпадает "
                    f"с порогом «{mind[r.from_change_mind - 1].threshold}» записи {r.from_change_mind} "
                    f"из recommendation.change_mind ({OPTIONS_FILE})"
                )
    for n, c in enumerate(ver.premortem, start=1):
        where = f"{VERIFICATION_FILE}, premortem, запись №{n}"
        cl.ref(where, "claim", c.claim, type_="гипотеза", synthetic_note=synthetic)
        own.check(errors, where, "row", c.row)
        if kinds.get(c.row) == "основной":
            errors.append(
                f"{where}, поле row: строка {c.row} основная, а причина пре-мортема должна вести "
                "в защитный показатель"
            )
    for n, u in enumerate(ver.undesired_effects, start=1):
        own.check(errors, f"{VERIFICATION_FILE}, undesired_effects, запись №{n}", "row", u.row)


def _check_decisions(
    dec: DecisionsFile,
    cl: _Claims,
    opt_reg: _Registry,
    ver_reg: _Registry,
    ver: VerificationFile | None,
    errors: list[str],
) -> None:
    from strateg.project import now  # импорт здесь: project импортирует этот модуль

    today = str(now().date())  # день в поясе проекта; даты хранятся строками вида 2026-11-06
    _dup_ids(DECISIONS_FILE, [d.id for d in dec.decisions], errors)
    own = _own(DECISIONS_FILE, [d.id for d in dec.decisions])
    by_id = {d.id: d for d in dec.decisions}
    for n, d in enumerate(dec.decisions, start=1):
        where = f"{DECISIONS_FILE}, запись {d.id} (№{n})"
        opt_reg.check(errors, where, "option", d.option)
        for ref in d.rejected:
            opt_reg.check(errors, where, "rejected", ref)
        for ref in d.basis:
            cl.ref(where, "basis", ref, synthetic_note=_SYNTHETIC_PROOF)
        for ref in d.assumptions:
            cl.ref(where, "assumptions", ref, type_=("допущение", "гипотеза"))
        rows = {r.id: r for r in ver.rows} if ver else {}
        for i, c in enumerate(d.conditions):
            if c.row is None:
                continue
            ver_reg.check(errors, where, f"conditions[{i}].row", c.row)
            row = rows.get(c.row)
            if row is not None and not _same_threshold(c.threshold, row.change_mind.threshold):
                errors.append(
                    f"{where}, поле conditions[{i}].row: порог условия «{c.threshold}» не совпадает "
                    f"с порогом «{row.change_mind.threshold}» строки {c.row} из {VERIFICATION_FILE}"
                )
        for field, ref in (("replaces", d.replaces), ("replaced_by", d.replaced_by)):
            if ref:
                own.check(errors, where, field, ref)
        if d.replaced_by in by_id and by_id[d.replaced_by].replaces != d.id:
            errors.append(f"{where}: у {d.replaced_by} нет обратной ссылки replaces: {d.id}")
        if d.replaces in by_id:
            old = by_id[d.replaces]
            if old.status != "заменено":
                errors.append(f"{where}: заменяет {old.id}, но у него статус «{old.status}», а не «заменено»")
            if old.replaced_by != d.id:
                errors.append(f"{where}: у {old.id} нет ссылки replaced_by: {d.id}")
        for i, cm in enumerate(d.commitments):
            if cm.done is not None and cm.done > today:
                errors.append(
                    f"{where}, поле commitments[{i}].done: дата {cm.done} в будущем (сегодня {today}): "
                    "выполнение отмечают на дату, когда оно уже было"
                )
    cycle = _find_cycle({d.id: [d.replaced_by] if d.replaced_by else [] for d in dec.decisions})
    if cycle:
        errors.append(f"{DECISIONS_FILE}: цикл замены: {' -> '.join(cycle)}")


def _check_metrics(metrics: MetricsFile, ver_reg: _Registry, errors: list[str]) -> None:
    """Значение показателя ведёт в существующую строку плана проверки."""
    for n, v in enumerate(metrics.values, start=1):
        ver_reg.check(errors, f"{METRICS_FILE}, запись №{n} ({v.row})", "row", v.row)


def _check_plan_questions(claims: ClaimsFile, plan_reg: _Registry, errors: list[str]) -> None:
    """`plan_question` — строка; значение вида ВП-n должно быть в плане, прочие тексты допустимы.

    Значение, похожее на номер (латинские буквы, строчные, ноль впереди, несколько номеров), — ошибка
    с подсказкой: принятое молча за текст, оно оставило бы утверждение без связи с планом.
    """
    for n, c in enumerate(claims.claims, start=1):
        value = (c.plan_question or "").strip()
        where = f"{CLAIMS_FILE}, запись {c.id} (№{n})"
        if _PLAN_REF.fullmatch(value):
            plan_reg.check(errors, where, "plan_question", value)
        elif _PLAN_LIKE.fullmatch(value):
            errors.append(
                f"{where}, поле plan_question: значение «{value}» не подходит: нужен один номер вопроса "
                f"плана вида ВП-1 (буквы русские заглавные, без нуля впереди){latin_hint(value)}"
            )


def _check_promise_marks(root: Path, prom_ok: bool, prom: PromisesFile | None, errors: list[str]) -> None:
    """Метка `[О-n]` в стратегии коммуникаций ведёт на существующее обещание."""
    path = root / COMMS_STRATEGY
    if not path.is_file() or (prom_ok and prom is None):
        return
    known = {p.id for p in prom.promises} if prom else set()
    text = path.read_bytes().decode("utf-8", errors="replace")
    for letter, number in dict.fromkeys(_PROMISE_MARK.findall(text)):
        mark = f"[{letter}-{number}]"
        if letter == "O":
            errors.append(
                f"{COMMS_STRATEGY}: метка {mark} написана латинской буквой O, а в номерах нужна русская О"
            )
        elif f"О-{number}" not in known:
            tail = "" if prom_ok else f": файла {PROMISES_FILE} нет"
            errors.append(f"{COMMS_STRATEGY}: метка {mark} не ведёт на обещание из {PROMISES_FILE}{tail}")


_SERVICE_NAMES = frozenset({"Thumbs.db", "desktop.ini"})


def _ranges(versions: list[int]) -> str:
    """Номера версий сжатым видом: `2…4, 7`."""
    out: list[str] = []
    start = prev = versions[0]
    for v in [*versions[1:], None]:
        if v is not None and v == prev + 1:
            prev = v
            continue
        out.append(str(start) if start == prev else f"{start}…{prev}")
        if v is not None:
            start = prev = v
    return ", ".join(out)


def _release_member(rest: tuple[str, ...]) -> bool:
    """Путь внутри `history/release-<n>/`: файл выхода или документ `outputs/<имя>.md`."""
    if len(rest) == 1:
        return rest[0] in OUTPUT_FILES
    return len(rest) == 2 and rest[0] == "outputs" and rest[1].endswith(".md")


def _check_history(root: Path, frame: FrameFile | None, errors: list[str]) -> None:
    """Имена версий в `history/`, полнота версий рамки и то, что прежняя версия — рамка."""
    from strateg.project import history_path, history_source  # импорт здесь: project импортирует этот модуль

    folder = root / HISTORY_DIR
    found: dict[int, str] = {}  # версии рамки, лежащие в history/
    if folder.is_dir():
        for p in sorted(folder.rglob("*")):
            rel = p.relative_to(root).as_posix()
            parts = p.relative_to(folder).parts
            service = any(part.startswith(".") for part in parts) or p.name in _SERVICE_NAMES
            if not p.is_file() or service:
                continue  # .gitkeep, .DS_Store и подобное — не версии
            if parts and _RELEASE_DIR.match(parts[0]):
                if not _release_member(parts[1:]):
                    errors.append(
                        f"{rel}: в снимке выпуска лежат файлы выхода и outputs/*.md, а это что-то другое"
                    )
                continue
            source = history_source(rel)
            if source is None:
                errors.append(
                    f"{rel}: имя не подходит для версии: нужен вид <файл>.v<N>.<расширение>, "
                    f"например {history_path(FRAME_FILE, 1)}; снимок выпуска лежит в "
                    f"history/{RELEASE_PREFIX}<n>/"
                )
            elif source[0] == FRAME_FILE:
                found[source[1]] = rel
    top = frame.version if frame is not None else None
    for v, rel in sorted(found.items()):
        if top is not None and v >= top:
            errors.append(
                f"{rel}: версия {v} не прежняя: у {FRAME_FILE} версия {top}, "
                f"в history/ лежат версии до {top - 1}"
            )
            continue
        raw = _load_yaml(root, rel, errors)
        old = _validate(rel, (), FrameFile, raw, errors) if raw is not None else None
        if isinstance(old, FrameFile) and old.version != v:
            errors.append(f"{rel}: в файле версия рамки {old.version}, а в имени {v}")
    if top is not None:
        missing = sorted(set(range(1, top)) - set(found))  # top не больше MAX_FRAME_VERSION
        if len(missing) == 1:
            errors.append(
                f"{FRAME_FILE}: версия {top}, но прежней версии {missing[0]} нет: "
                f"{history_path(FRAME_FILE, missing[0])}"
            )
        elif missing:
            errors.append(
                f"{FRAME_FILE}: версия {top}, но прежних версий нет: {_ranges(missing)} "
                f"(например {history_path(FRAME_FILE, missing[0])})"
            )


# ---------------------------------------------------------------------------
# Журнал и состояние
# ---------------------------------------------------------------------------


def _dump_field(value: Any) -> Any:
    return value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value


def frame_key(body: FrameBody) -> dict[str, Any]:
    """Поля рамки без служебных (`schema_version`, `version`): по ним рамка файла сравнивается с рамкой В1.

    Сравнивается содержимое: в событии В1 номера версии рамки нет.
    """
    return {name: _dump_field(getattr(body, name)) for name in FrameBody.model_fields}


def _frame_versions(root: Path, frame: FrameFile | None) -> list[FrameFile]:
    """Версии рамки, которые удалось разобрать: файл `frame.yaml` и файлы `history/frame.v<N>.yaml`."""
    from strateg.project import history_source  # импорт здесь: project импортирует этот модуль

    out: list[FrameFile] = [frame] if frame is not None else []
    folder = root / HISTORY_DIR
    if folder.is_dir():
        for p in sorted(folder.glob("*")):
            rel = p.relative_to(root).as_posix()
            source = history_source(rel)
            if source is None or source[0] != FRAME_FILE or not p.is_file():
                continue
            raw = _load_yaml(root, rel, [])
            old = _validate(rel, (), FrameFile, raw, []) if raw is not None else None
            if isinstance(old, FrameFile):
                out.append(old)
    return out


def _check_gate_frame(
    root: Path, frame: FrameFile | None, frame_present: bool, events: list[JournalEvent], errors: list[str]
) -> None:
    """Рамка последнего ответа на В1 совпадает с одной из версий рамки: файлом или `history/`."""
    answered = [
        (n, e.gate.frame)
        for n, e in enumerate(events, start=1)
        if e.gate is not None and e.gate.gate == "В1" and e.gate.answered_at is not None
    ]
    if not answered or answered[-1][1] is None or (frame_present and frame is None):
        return  # рамки в ответе нет или файл рамки не разобран: его ошибки уже названы
    n, body = answered[-1]
    versions = _frame_versions(root, frame)
    if not versions:
        return  # сравнивать не с чем: наличие файла рамки сверяет другое правило
    wanted = frame_key(body)
    if not any(frame_key(v) == wanted for v in versions):
        errors.append(
            f"{JOURNAL_FILE}, строка {n}: рамка в ответе на В1 не совпадает ни с {FRAME_FILE}, "
            f"ни с одной версией в {HISTORY_DIR}/; рамку после ответа правят новой версией, "
            "а прежняя остаётся в history/"
        )


def _check_journal_rules(
    state: ProjectState | None,
    sources: SourcesFile | None,
    events: list[JournalEvent],
    errors: list[str],
) -> None:
    """Связи журнала с реестром источников и состоянием."""
    for n, (prev, cur) in enumerate(zip(events, events[1:], strict=False), start=2):
        if cur.time < prev.time:
            errors.append(
                f"{JOURNAL_FILE}, строка {n}: время события раньше предыдущей строки; "
                "журнал только дописывается, время не убывает"
            )

    if sources is not None:
        known = {s.id for s in sources.sources}
        suspicious = {s.id for s in sources.sources if s.suspicious}
        flagged: set[str] = set()
        for n, e in enumerate(events, start=1):
            if e.kind != "найдена инструкция в источнике" or e.source is None:
                continue
            flagged.add(e.source)
            if e.source in known and e.source not in suspicious:
                errors.append(
                    f"{JOURNAL_FILE}, строка {n}: найдена инструкция в источнике {e.source}, "
                    f"а он не помечен «подозрительный» в {SOURCES_FILE}"
                )
        for sid in sorted(suspicious - flagged, key=lambda x: int(x.split("-")[1])):
            errors.append(
                f"{SOURCES_FILE}, запись {sid}: помечен «подозрительный», а события «найдена инструкция "
                "в источнике» в журнале нет"
            )

    if state is None:
        return
    last: tuple[int, JournalEvent] | None = None
    for n, e in enumerate(events, start=1):
        if e.kind == "действие" and (e.name or "").startswith("шаг ") and e.status in ("начато", "выполнено"):
            last = (n, e)
    if last is not None and last[1].step != state.step:
        errors.append(
            f"{STATE_FILE}, поле step: «{state.step}», а последний начатый или выполненный шаг в журнале — "
            f"«{last[1].step}» (строка {last[0]})"
        )
    for gate in ("В1", "В2", "В3", "В4", "В5"):
        mine = [e.gate for e in events if e.gate is not None and e.gate.gate == gate]
        if not mine:
            want = "впереди"
        elif mine[-1].answered_at is not None:
            want = "отвечено"
        elif mine[-1].questions == 0:
            want = "пройдены автоматически"
        else:
            want = "ждут ответа"
        have = state.gates.get(gate)  # type: ignore[call-overload]
        if have != want:
            errors.append(
                f"{STATE_FILE}, поле gates.{gate}: «{have}», а по событиям журнала ворота {gate} — «{want}»"
            )


def check_project(root: Path | str) -> list[str]:
    """Проверяет папку проекта и возвращает список ошибок по-русски; пустой список — всё в порядке."""
    root = Path(root)
    errors: list[str] = []
    if not root.is_dir():
        return [f"папка проекта не найдена: {root.name}"]

    state_raw = _load_yaml(root, STATE_FILE, errors)
    sources_raw = _load_yaml(root, SOURCES_FILE, errors)
    claims_raw = _load_yaml(root, CLAIMS_FILE, errors)

    state = _validate(STATE_FILE, None, ProjectState, state_raw, errors) if state_raw is not None else None
    sources = _validate(SOURCES_FILE, "sources", SourcesFile, sources_raw, errors) if sources_raw else None
    claims = _validate(CLAIMS_FILE, "claims", ClaimsFile, claims_raw, errors) if claims_raw else None

    if isinstance(sources, SourcesFile):
        for dup in sorted(_duplicates([s.id for s in sources.sources])):
            errors.append(f"{SOURCES_FILE}: номер {dup} встречается больше одного раза")
        known = {s.id for s in sources.sources}
        for n, s in enumerate(sources.sources, start=1):
            if s.refreshes is not None and s.refreshes not in known:
                errors.append(
                    f"{SOURCES_FILE}, запись {s.id} (№{n}), поле refreshes: несуществующий источник "
                    f"{s.refreshes}"
                )
        _check_sources_files(root, sources, errors)
        _check_unreferenced_files(root, sources, errors)
    if isinstance(sources, SourcesFile) and isinstance(claims, ClaimsFile):
        _check_claims(root, sources, claims, errors)

    frame_present, frame = _check_outputs(
        root,
        sources if isinstance(sources, SourcesFile) else None,
        claims if isinstance(claims, ClaimsFile) else None,
        errors,
    )

    for rel, model, check in (
        (JOURNAL_FILE, JournalEvent, "events"),
        (COST_FILE, CostRecord, "costs"),
    ):
        records = _load_jsonl(root, rel, model, errors)
        if records is None:
            continue
        if check == "events":
            known_sources = sources if isinstance(sources, SourcesFile) else None
            _check_events(root, records, known_sources, errors, frame_present)
            _check_journal_rules(
                state if isinstance(state, ProjectState) else None, known_sources, records, errors
            )
            _check_gate_frame(root, frame, frame_present, records, errors)
        else:
            _check_costs(records, errors)
    return errors
