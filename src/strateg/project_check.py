"""Проверка папки проекта целиком: `check_project(папка)` возвращает список ошибок по-русски.

Каждая ошибка называет файл и запись: `claims.yaml, запись У-3 (№3), поле quotes[0].text: ...`.
Форму записей проверяют модели из `strateg.models`; здесь — чтение файлов (UTF-8, LF), перевод
сообщений pydantic на русский и правила между записями и файлами: ссылки на существующие номера,
замена утверждений, циклы в основаниях и замене, снимки и их хеши, повторное вхождение цитаты в текст
снимка, повторные отметки выполнения.

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
    ClaimsFile,
    CostRecord,
    JournalEvent,
    ProjectState,
    Source,
    SourcesFile,
    kind_of,
    norm,
    quote_in_snapshot,
    show,
)

STATE_FILE = "state.yaml"
SOURCES_FILE = "sources.yaml"
CLAIMS_FILE = "claims.yaml"
JOURNAL_FILE = "journal.jsonl"
COST_FILE = "cost.jsonl"
PROJECT_FILES = (STATE_FILE, SOURCES_FILE, CLAIMS_FILE, JOURNAL_FILE, COST_FILE)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Сообщения pydantic по-русски
# ---------------------------------------------------------------------------

_PATTERN_HINTS = {
    r"^И-[1-9][0-9]*$": "номер источника вида И-1 (буква русская)",
    r"^У-[1-9][0-9]*$": "номер утверждения вида У-1 (буква русская)",
    r"^[0-9a-f]{64}$": "sha256: 64 символа 0-9 и a-f, строчными",
    r"^https?://[^\s]+$": "адрес, начинающийся с http:// или https://",
    r"^[^\s]+ [0-9][^\s]*$": "название и версия через пробел, например «trafilatura 2.0.0»",
    r"^[a-z0-9][a-z0-9_-]*$": "латинские строчные буквы, цифры, «-» и «_»",
}
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
        allowed = str(ctx.get("expected")).replace(" or ", ", ")
        return [f"недопустимое значение «{show(err.get('input'))}»; допустимо: {allowed}{hint}"]
    if t == "string_type":
        return [f"ожидается строка, а получено: {kind_of(err.get('input'))}; {_QUOTE_HINT}"]
    if t == "string_pattern_mismatch":
        hint = _PATTERN_HINTS.get(ctx.get("pattern", ""), f"шаблон {ctx.get('pattern')}")
        return [f"значение «{show(err.get('input'))}» не подходит: нужно {hint}"]
    if t == "string_too_short":
        return ["строка пустая"]
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


_RECORD_ID = re.compile(r"^[УИ]-[1-9][0-9]*$")


def _record_label(file: str, section: str | None, raw: Any, index: int) -> str:
    rec = None
    if section and isinstance(raw, dict) and isinstance(raw.get(section), list) and index < len(raw[section]):
        rec = raw[section][index]
    ident = rec.get("id") if isinstance(rec, dict) else None
    if isinstance(ident, str) and _RECORD_ID.match(ident):
        return f"{file}, запись {ident} (№{index + 1})"
    return f"{file}, запись №{index + 1}"


def _format_errors(file: str, section: str | None, raw: Any, exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = list(err["loc"])
        label = file
        if section and len(loc) >= 2 and loc[0] == section and isinstance(loc[1], int):
            label = _record_label(file, section, raw, loc[1])
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
    rel: str, section: str | None, model: type[BaseModel], raw: Any, errors: list[str]
) -> BaseModel | None:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        errors.extend(_format_errors(rel, section, raw, exc))
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
            records.append(model.model_validate(raw))
        except ValidationError as exc:
            ok = False
            errors.extend(
                m.replace(rel, f"{rel}, строка {n}", 1) for m in _format_errors(rel, None, raw, exc)
            )
    return records if ok else None


# ---------------------------------------------------------------------------
# Правила между записями и файлами
# ---------------------------------------------------------------------------


def _duplicates(ids: list[str]) -> set[str]:
    return {i for i in ids if ids.count(i) > 1}


def _check_sources_files(root: Path, sources: SourcesFile, errors: list[str]) -> None:
    for n, s in enumerate(sources.sources, start=1):
        label = f"{SOURCES_FILE}, запись {s.id} (№{n})"
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
            # Запись проверки перепроверяется по тексту снимка. «Нечёткое» сходство пересчитывает
            # `quote check` (пакет 3.2): здесь только порог из модели.
            if q.check is not None and q.check.snapshot_sha256 == src.text_sha256:
                text = _snapshot_text(root, src, snapshots)
                if (
                    text is not None
                    and q.check.method != "нечёткое"
                    and not quote_in_snapshot(q.check.method, q.text, text)
                ):
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


def _check_events(events: list[JournalEvent], sources: SourcesFile | None, errors: list[str]) -> None:
    done: dict[str, int] = {}
    ids = {s.id for s in sources.sources} if sources else None
    for n, e in enumerate(events, start=1):
        h = e.idempotency_hash
        if e.kind == "действие" and e.status == "выполнено" and h:
            if h in done:
                errors.append(
                    f"{JOURNAL_FILE}, строка {n}: действие уже выполнено (строка {done[h]}), "
                    "повторная отметка выполнения с тем же ключом"
                )
            done.setdefault(h, n)
        if e.source and ids is not None and e.source not in ids:
            errors.append(f"{JOURNAL_FILE}, строка {n}: ссылка на несуществующий источник {e.source}")


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


def check_project(root: Path | str) -> list[str]:
    """Проверяет папку проекта и возвращает список ошибок по-русски; пустой список — всё в порядке."""
    root = Path(root)
    errors: list[str] = []
    if not root.is_dir():
        return [f"папка проекта не найдена: {root.name}"]

    state_raw = _load_yaml(root, STATE_FILE, errors)
    sources_raw = _load_yaml(root, SOURCES_FILE, errors)
    claims_raw = _load_yaml(root, CLAIMS_FILE, errors)

    if state_raw is not None:
        _validate(STATE_FILE, None, ProjectState, state_raw, errors)
    sources = _validate(SOURCES_FILE, "sources", SourcesFile, sources_raw, errors) if sources_raw else None
    claims = _validate(CLAIMS_FILE, "claims", ClaimsFile, claims_raw, errors) if claims_raw else None

    if isinstance(sources, SourcesFile):
        for dup in sorted(_duplicates([s.id for s in sources.sources])):
            errors.append(f"{SOURCES_FILE}: номер {dup} встречается больше одного раза")
        _check_sources_files(root, sources, errors)
    if isinstance(sources, SourcesFile) and isinstance(claims, ClaimsFile):
        _check_claims(root, sources, claims, errors)

    for rel, model, check in (
        (JOURNAL_FILE, JournalEvent, "events"),
        (COST_FILE, CostRecord, "costs"),
    ):
        records = _load_jsonl(root, rel, model, errors)
        if records is None:
            continue
        if check == "events":
            _check_events(records, sources if isinstance(sources, SourcesFile) else None, errors)
        else:
            _check_costs(records, errors)
    return errors
