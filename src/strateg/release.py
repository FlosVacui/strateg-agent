"""Снимок выпуска и разница «было → стало» после него (ADR-0016, порядок пересчёта, п. 4).

При `step done 7` CLI кладёт в `history/release-<n>/` файлы выхода и документы `outputs/*.md` такими, какими
они были на выпуске. Дальше `strateg diff` сравнивает снимок с тем, что лежит в проекте сейчас: по файлам
выхода — по номерам записей, по документам — по разделам (заголовки `##`). Снимок хранится как прочая
история: он входит в хеши `state.yaml` и в архив `pack`.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strateg import project as proj
from strateg import storage
from strateg.project import CliError, Project
from strateg.project_check import HISTORY_DIR, OUTPUT_FILES, RELEASE_PREFIX

OUTPUTS_DIR = "outputs"
_RELEASE = re.compile(rf"^{RELEASE_PREFIX}([1-9][0-9]*)$")
PREAMBLE = "начало документа"  # текст до первого заголовка `##`

# Файл выхода -> разделы, где записи различаются по номеру (ключ записи), и скалярные поля верхнего уровня.
# Остальные ключи файла (кроме schema_version) сравниваются целиком как один блок.
RECORDS: dict[str, dict[str, str]] = {
    "plan.yaml": {"questions": "id"},
    "gaps.yaml": {"gaps": "id", "metric_coverage": "kind"},
    "options.yaml": {"options": "id"},
    "promises.yaml": {"promises": "id"},
    "verification.yaml": {"rows": "id"},
    "decisions.yaml": {"decisions": "id"},
}
RECORD_TITLE = {
    "plan.yaml": "План исследования",
    "gaps.yaml": "Пробелы",
    "options.yaml": "Варианты",
    "promises.yaml": "Обещания",
    "verification.yaml": "План проверки",
    "decisions.yaml": "Решения",
    "frame.yaml": "Рамка",
}
BLOCK_TITLE = {
    ("options.yaml", "recommendation"): "рекомендация",
    ("options.yaml", "chosen"): "выбор на В4",
    ("verification.yaml", "premortem"): "пре-мортем",
    ("verification.yaml", "undesired_effects"): "нежелательные эффекты",
    ("plan.yaml", "excluded"): "исключённые вопросы",
    ("plan.yaml", "estimate"): "оценка пути",
}


def release_dirs(root: Path) -> dict[int, Path]:
    """Снимки выпуска проекта: номер -> папка."""
    folder = root / HISTORY_DIR
    if not folder.is_dir():
        return {}
    found = {}
    for p in folder.iterdir():
        m = _RELEASE.match(p.name)
        if m and p.is_dir():
            found[int(m.group(1))] = p
    return dict(sorted(found.items()))


def snapshot_sources(root: Path) -> list[str]:
    """Файлы проекта, которые войдут в снимок: файлы выхода и документы `outputs/*.md`."""
    rels = [name for name in OUTPUT_FILES if (root / name).is_file()]
    docs = root / OUTPUTS_DIR
    if docs.is_dir():
        rels += [f"{OUTPUTS_DIR}/{p.name}" for p in sorted(docs.glob("*.md")) if p.is_file()]
    return rels


def take_snapshot(p: Project) -> tuple[int, list[str]]:
    """Кладёт снимок выпуска в `history/release-<n>/`: (номер, файлы). Нет файлов — снимка нет, номер 0.

    Вызывать внутри `transaction`: при откате папка убирается вместе с остальным.
    """
    rels = snapshot_sources(p.root)
    if not rels:
        return 0, []
    taken = release_dirs(p.root)
    n = max(taken, default=0) + 1
    base = p.root / HISTORY_DIR / f"{RELEASE_PREFIX}{n}"
    for rel in rels:
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        storage.atomic_write_bytes(dest, (p.root / rel).read_bytes())  # байт в байт
    return n, rels


# ---------------------------------------------------------------------------
# Разница
# ---------------------------------------------------------------------------


@dataclass
class RecordChange:
    id: str
    fields: list[dict[str, Any]] = field(default_factory=list)  # {field, was, now}


@dataclass
class FileDiff:
    file: str
    title: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[RecordChange] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)  # {block, was, now}

    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.blocks)


@dataclass
class DocDiff:
    path: str
    status: str  # «изменён», «добавлен», «удалён»
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return proj.read_yaml(path)
    except CliError:
        return None


def released_decisions(root: Path) -> tuple[int, list[str]] | None:
    """Номер последнего выпуска и номера решений, которые на нём были; снимка или файла решений нет — None."""
    snaps = release_dirs(root)
    if not snaps:
        return None
    n = max(snaps)
    data = _yaml(snaps[n] / "decisions.yaml")
    if data is None:
        return None
    ids = [str(x["id"]) for x in data.get("decisions", []) if isinstance(x, dict) and "id" in x]
    return n, ids


def _plain(value: Any) -> Any:
    """Значение для JSON: даты YAML становятся строками."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _field_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [k for k in (*old, *new) if k != "schema_version"]
    out = []
    for k in dict.fromkeys(keys):
        if old.get(k) != new.get(k):
            out.append({"field": k, "was": _plain(old.get(k)), "now": _plain(new.get(k))})
    return out


def _by_key(items: Any, key: str) -> dict[str, dict[str, Any]]:
    return {str(i.get(key)): i for i in items or [] if isinstance(i, dict) and i.get(key) is not None}


def diff_yaml(name: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> FileDiff:
    """Разница двух версий файла выхода: добавленные, снятые и изменённые записи по номеру."""
    d = FileDiff(name, RECORD_TITLE[name])
    if old == new:
        return d
    old = old or {}
    new = new or {}
    sections = RECORDS.get(name, {})
    if name == "frame.yaml":
        for ch in _field_changes({k: v for k, v in old.items() if k != "version"}, new):
            if ch["field"] != "version":
                d.changed.append(RecordChange(ch["field"], [ch]))
        return d
    for section, key in sections.items():
        a, b = _by_key(old.get(section), key), _by_key(new.get(section), key)
        d.added += [i for i in b if i not in a]
        d.removed += [i for i in a if i not in b]
        for i in a:
            if i in b and a[i] != b[i]:
                d.changed.append(RecordChange(i, _field_changes(a[i], b[i])))
    for k in dict.fromkeys([*old, *new]):
        if k == "schema_version" or k in sections:
            continue
        if old.get(k) != new.get(k):
            d.blocks.append(
                {"block": BLOCK_TITLE.get((name, k), k), "was": _plain(old.get(k)), "now": _plain(new.get(k))}
            )
    return d


def sections_of(text: str) -> dict[str, str]:
    """Разделы документа: заголовок `##` -> текст раздела; текст до первого заголовка — «начало документа»."""
    out: dict[str, list[str]] = {PREAMBLE: []}
    current = PREAMBLE
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            out.setdefault(current, [])
        else:
            out[current].append(line)
    return {k: _squeeze(v) for k, v in out.items() if k != PREAMBLE or "".join(v).strip()}


def _squeeze(lines: list[str]) -> str:
    """Текст раздела без хвостовых пробелов и лишних пустых строк: на разницу они не влияют."""
    text = "\n".join(line.rstrip() for line in lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def diff_doc(path: str, old: str | None, new: str | None) -> DocDiff | None:
    if old == new:
        return None
    if old is None:
        return DocDiff(path, "добавлен", added=list(sections_of(new or "")))
    if new is None:
        return DocDiff(path, "удалён", removed=list(sections_of(old)))
    a, b = sections_of(old), sections_of(new)
    d = DocDiff(
        path,
        "изменён",
        added=[s for s in b if s not in a],
        removed=[s for s in a if s not in b],
        changed=[s for s in a if s in b and a[s] != b[s]],
    )
    if not (d.added or d.removed or d.changed):
        return None  # отличаются только пробелы и пустые строки
    return d


@dataclass
class Diff:
    release: int
    files: list[FileDiff]
    docs: list[DocDiff]

    def empty(self) -> bool:
        return not (self.files or self.docs)


def _text(path: Path) -> str | None:
    return path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else None


def compare(root: Path, release: int) -> Diff:
    """Снимок выпуска `release` против проекта сейчас."""
    base = release_dirs(root)[release]
    files = []
    for name in OUTPUT_FILES:
        d = diff_yaml(name, _yaml(base / name), _yaml(root / name))
        if not d.empty():
            files.append(d)
    names = (
        {f"{OUTPUTS_DIR}/{p.name}" for p in (base / OUTPUTS_DIR).glob("*.md")}
        if (base / OUTPUTS_DIR).is_dir()
        else set()
    )
    now = root / OUTPUTS_DIR
    names |= {f"{OUTPUTS_DIR}/{p.name}" for p in now.glob("*.md")} if now.is_dir() else set()
    docs = []
    for rel in sorted(names):
        d = diff_doc(rel, _text(base / rel), _text(root / rel))
        if d is not None:
            docs.append(d)
    return Diff(release, files, docs)
