"""Проверка папки проекта целиком: `check_project(папка)` возвращает список ошибок по-русски.

Каждая ошибка называет файл и запись: `claims.yaml, запись У-3 (№3), поле quotes[0].text: ...`.
Форму записей проверяют модели из `strateg.models`; здесь — чтение файлов (UTF-8, LF), перевод
сообщений pydantic на русский и правила между записями и файлами: ссылки на существующие номера,
замена утверждений, циклы в основаниях, снимки и их хеши, повторные отметки выполнения.
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
    SourcesFile,
    norm,
)

STATE_FILE = "state.yaml"
SOURCES_FILE = "sources.yaml"
CLAIMS_FILE = "claims.yaml"
JOURNAL_FILE = "journal.jsonl"
COST_FILE = "cost.jsonl"
PROJECT_FILES = (STATE_FILE, SOURCES_FILE, CLAIMS_FILE, JOURNAL_FILE, COST_FILE)

# Абсолютные пути в тексте: диск Windows, сетевой путь, домашние и системные папки Unix.
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?<![\w.:\\])\\\\[^\s\\]+\\"
    r"|(?<![\w.:/~-])/(?:Users|home|tmp|var|etc|mnt|root|opt)/"
)
# Поля адресов и путей проверяют сами модели; в остальном тексте ищем абсолютные пути.
_SKIP_KEYS = {"url", "final_url", "path", "text_path"}


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
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (("." if out else "") + str(part))
    return out


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
        return [f"версия схемы «{err.get('input')}» не поддерживается; ожидается {ctx.get('expected')}"]
    if t == "literal_error":
        hint = "" if isinstance(err.get("input"), str) else f"; {_QUOTE_HINT}"
        allowed = str(ctx.get("expected")).replace(" or ", ", ")
        return [f"недопустимое значение «{err.get('input')}»; допустимо: {allowed}{hint}"]
    if t == "string_type":
        return [f"ожидается строка, а получено {type(err.get('input')).__name__}; {_QUOTE_HINT}"]
    if t == "string_pattern_mismatch":
        hint = _PATTERN_HINTS.get(ctx.get("pattern", ""), f"шаблон {ctx.get('pattern')}")
        return [f"значение «{err.get('input')}» не подходит: нужно {hint}"]
    if t == "string_too_short":
        return ["строка пустая"]
    if t in ("bool_type", "bool_parsing"):
        return ["ожидается логическое значение true или false"]
    if t in ("int_type", "int_parsing", "int_from_float"):
        return ["ожидается целое число"]
    if t in ("float_type", "float_parsing"):
        return ["ожидается число"]
    if t in ("greater_than_equal", "greater_than", "less_than_equal", "less_than"):
        return [f"число {err.get('input')} вне допустимых границ"]
    if t == "timezone_aware":
        return ["время без часового пояса: добавьте пояс, например 2026-11-06T10:00:00+03:00"]
    if t.startswith("datetime") or t.startswith("date_"):
        return ["ожидаются дата и время в формате ISO 8601 с поясом, например 2026-11-06T10:00:00+03:00"]
    if t in ("list_type", "set_type", "tuple_type"):
        return ["ожидается список"]
    if t in ("dict_type", "model_type", "model_attributes_type"):
        return ["ожидается набор полей «ключ: значение»"]
    return [f"некорректное значение ({t})"]


def _record_label(file: str, section: str | None, raw: Any, index: int) -> str:
    rec = None
    if section and isinstance(raw, dict) and isinstance(raw.get(section), list) and index < len(raw[section]):
        rec = raw[section][index]
    ident = rec.get("id") if isinstance(rec, dict) else None
    return f"{file}, запись {ident} (№{index + 1})" if ident else f"{file}, запись №{index + 1}"


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


def _load_yaml(root: Path, rel: str, errors: list[str]) -> Any | None:
    text = _read_text(root, rel, errors)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text)
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
    for n, line in enumerate(text.split("\n"), start=1):
        if n == len(text.split("\n")) and not line:
            break  # завершающий перевод строки
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


def _scan_abs_paths(rel: str, node: Any, errors: list[str], trail: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in _SKIP_KEYS:
                _scan_abs_paths(rel, v, errors, f"{trail}.{k}" if trail else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_abs_paths(rel, v, errors, f"{trail}[{i}]")
    elif isinstance(node, str) and _ABS_PATH_RE.search(node):
        errors.append(f"{rel}, поле {trail}: в тексте абсолютный путь; внутри проекта только относительные")


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


def _basis_cycle(claims: dict[str, Any]) -> list[str] | None:
    """Цикл в основаниях: список номеров по кругу или None."""
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(cid: str) -> list[str] | None:
        state[cid] = 1
        stack.append(cid)
        for nxt in claims[cid].basis:
            if nxt not in claims:
                continue
            if state.get(nxt) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if nxt not in state and (found := visit(nxt)):
                return found
        stack.pop()
        state[cid] = 2
        return None

    for cid in claims:
        if cid not in state and (found := visit(cid)):
            return found
    return None


def _check_claims(sources: SourcesFile, claims: ClaimsFile, errors: list[str]) -> None:
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

    cycle = _basis_cycle(by_id)
    if cycle:
        errors.append(f"{CLAIMS_FILE}: цикл в основаниях: {' -> '.join(cycle)}")


def _check_events(events: list[JournalEvent], sources: SourcesFile | None, errors: list[str]) -> None:
    done: dict[str, int] = {}
    ids = {s.id for s in sources.sources} if sources else None
    for n, e in enumerate(events, start=1):
        if e.kind == "действие" and e.status == "выполнено" and e.key:
            if e.key in done:
                errors.append(
                    f"{JOURNAL_FILE}, строка {n}: действие уже выполнено (строка {done[e.key]}), "
                    "повторная отметка выполнения с тем же ключом"
                )
            done.setdefault(e.key, n)
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
    for rel, raw in ((STATE_FILE, state_raw), (SOURCES_FILE, sources_raw), (CLAIMS_FILE, claims_raw)):
        if raw is not None:
            _scan_abs_paths(rel, raw, errors)

    if state_raw is not None:
        _validate(STATE_FILE, None, ProjectState, state_raw, errors)
    sources = _validate(SOURCES_FILE, "sources", SourcesFile, sources_raw, errors) if sources_raw else None
    claims = _validate(CLAIMS_FILE, "claims", ClaimsFile, claims_raw, errors) if claims_raw else None

    if isinstance(sources, SourcesFile):
        for dup in sorted(_duplicates([s.id for s in sources.sources])):
            errors.append(f"{SOURCES_FILE}: номер {dup} встречается больше одного раза")
        _check_sources_files(root, sources, errors)
    if isinstance(sources, SourcesFile) and isinstance(claims, ClaimsFile):
        _check_claims(sources, claims, errors)

    for rel, model, check in (
        (JOURNAL_FILE, JournalEvent, "events"),
        (COST_FILE, CostRecord, "costs"),
    ):
        records = _load_jsonl(root, rel, model, errors)
        if records is None:
            continue
        for n, rec in enumerate(records, start=1):
            _scan_abs_paths(f"{rel}, строка {n}", rec.model_dump(mode="json"), errors)
        if check == "events":
            _check_events(records, sources if isinstance(sources, SourcesFile) else None, errors)
        else:
            _check_costs(records, errors)
    return errors


# ---------------------------------------------------------------------------
# Вычисляемое по файлам (ничего из этого не хранится)
# ---------------------------------------------------------------------------


def work_minutes(events: Iterable[JournalEvent]) -> float:
    """Время работы агента по ADR: сумма длительностей выполненных действий; ожидание ответа не входит."""
    return sum(e.duration_s or 0.0 for e in events if e.kind == "действие" and e.status == "выполнено") / 60


def total_cost_usd(records: Iterable[CostRecord]) -> float | None:
    """Стоимость проекта: по сессии — последний «итог сессии», а если его нет, сумма записей по сообщениям.

    None — ни в одной записи нет стоимости.
    """
    sessions: dict[str, dict[str, Any]] = {}
    for r in records:
        s = sessions.setdefault(r.session_id, {"total": None, "sum": None})
        if r.cost_usd is None:
            continue
        if r.entry == "итог сессии":
            s["total"] = r.cost_usd
        else:
            s["sum"] = (s["sum"] or 0.0) + r.cost_usd
    values = [s["total"] if s["total"] is not None else s["sum"] for s in sessions.values()]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def changed_outside_agent(root: Path | str) -> list[str]:
    """Файлы из `state.yaml: files`, чей sha256 не совпал или которых больше нет."""
    root = Path(root)
    state = ProjectState.model_validate(yaml.safe_load((root / STATE_FILE).read_text(encoding="utf-8")))
    return [
        rel
        for rel, digest in state.files.items()
        if not (root / rel).is_file() or sha256_file(root / rel) != digest
    ]
