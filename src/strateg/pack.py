"""Упаковка проекта для передачи (ADR-0003, ADR-0011, п. 4): архив `.zip` папки проекта.

Порядок: проверка проекта (`check`), поиск секретов, проверка, что исходников с персональными данными
в папке нет, затем архив. В архив не входят `.tmp/` (там лежит исходник до подтверждения замен),
`.cache/` (кэш `fetch`) и временные файлы. Найден секрет — отказ с файлом и строкой; само значение не
показывается. Встроенные шаблоны работают всегда, `gitleaks` добавляется, если он есть в PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from strateg import keys
from strateg import project as proj
from strateg.project import CliError, UsageError
from strateg.project_check import check_project

EXCLUDED_DIRS = frozenset({".tmp", ".cache", "__pycache__", ".git"})
EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
EXCLUDED_SUFFIXES = (".tmp", ".bak", ".swp")
MAX_SCAN_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    file: str  # путь от корня проекта
    line: int
    what: str

    def text(self) -> str:
        return f"{self.file}, строка {self.line}: {self.what}"


def project_files(root: Path) -> list[Path]:
    """Файлы, которые войдут в архив: без `.tmp/`, `.cache/` и временных файлов."""
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if EXCLUDED_DIRS.intersection(parts[:-1]) or path.name in EXCLUDED_NAMES:
            continue
        if path.name.endswith(EXCLUDED_SUFFIXES) or path.name.startswith("~$"):
            continue
        out.append(path)
    return out


def _text_of(path: Path) -> str | None:
    """Текст файла для поиска секретов; None — двоичный или слишком большой."""
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(text: str) -> list[tuple[int, str]]:
    """Совпадения встроенных шаблонов: (строка, название шаблона). Шаблоны общие с проверками до модели."""
    return keys.scan_lines(text)


def scan_builtin(root: Path, files: list[Path]) -> list[Finding]:
    found = []
    for path in files:
        text = _text_of(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        found += [Finding(rel, n, f"похоже на секрет: {name}") for n, name in scan_text(text)]
    return found


GITLEAKS_USED = "использован"
GITLEAKS_MISSING = "не найден"
GITLEAKS_FAILED = "не отработал"


def scan_gitleaks(root: Path, files: list[Path]) -> tuple[list[Finding], str]:
    """Сканер секретов gitleaks, если он есть в PATH: (находки, состояние сканера).

    Состояние: «использован», «не найден», «не отработал» (не запустился, вышел с ошибкой или по таймауту,
    отчёт не прочитан). Значения не выводятся (`--redact`).
    """
    exe = shutil.which("gitleaks")
    if exe is None:
        return [], GITLEAKS_MISSING
    included = {p.relative_to(root).as_posix() for p in files}
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        base = [exe, "--no-banner", "--redact", "--report-format", "json", "--report-path", str(report)]
        for command in (["dir", str(root)], ["detect", "--no-git", "--source", str(root)]):
            report.unlink(missing_ok=True)
            try:
                done = subprocess.run(
                    [base[0], command[0], *base[1:], *command[1:]],
                    capture_output=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return [], GITLEAKS_FAILED
            if done.returncode in (0, 1) and (report.is_file() or done.returncode == 0):
                break
        else:
            return [], GITLEAKS_FAILED
        if not report.is_file():
            return [], GITLEAKS_FAILED
        try:
            leaks = json.loads(report.read_text(encoding="utf-8") or "[]")
        except (OSError, ValueError):
            return [], GITLEAKS_FAILED
    found = []
    for leak in leaks or []:
        file = str(leak.get("File", ""))
        try:
            rel = Path(file).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = file.replace("\\", "/")
        if rel in included:
            found.append(
                Finding(rel, int(leak.get("StartLine") or 0), f"gitleaks: {leak.get('RuleID', 'секрет')}")
            )
    return found, GITLEAKS_USED


def personal_originals(root: Path, files: list[Path]) -> list[str]:
    """Файлы в папке проекта, совпадающие с исходником материала с персональными данными."""
    from strateg.project import Project

    p = Project.load(root)
    hashes = {s.material_sha256: s.id for s in p.sources.sources if s.material_sha256 and s.anonymized}
    if not hashes:
        return []
    out = []
    for path in files:
        sha = proj.sha256_bytes(path.read_bytes())
        if sha in hashes:
            out.append(
                f"{path.relative_to(root).as_posix()}: исходник материала {hashes[sha]} "
                "с персональными данными"
            )
    return out


def verify(root: Path) -> tuple[list[Path], list[str], str]:
    """Все проверки до архива: (файлы архива, причины отказа, состояние gitleaks)."""
    with proj.locked(root):  # соседняя команда не пишет, пока проект проверяется: полузаписанного не видно
        problems = check_project(root)
    if problems:
        reasons = ["проверка проекта нашла ошибки (`strateg check`): " + m for m in problems]
        return [], reasons, GITLEAKS_MISSING
    files = project_files(root)
    builtin = scan_builtin(root, files)
    reasons = [f.text() for f in builtin]
    seen = {(f.file, f.line) for f in builtin}
    leaks, state = scan_gitleaks(root, files)
    reasons += [f.text() for f in leaks if (f.file, f.line) not in seen]
    reasons += personal_originals(root, files)
    return files, reasons, state


def build_archive(root: Path, files: list[Path], out: Path) -> int:
    """Архив `<id>/…`: распаковка даёт папку проекта. Запись через временный файл."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for path in files:
                arc = f"{root.name}/{path.relative_to(root).as_posix()}"
                z.write(path, arc)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return len(files)


def default_out(root: Path) -> Path:
    return proj.projects_root() / f"{root.name}.zip"


class Packed(NamedTuple):
    archive: Path
    files: int
    bytes: int
    gitleaks: str  # «использован», «не найден», «не отработал»


def pack(root: Path, out: Path | None) -> Packed:
    """Упаковывает проект. Отказ — `CliError` с причинами."""
    target = (out or default_out(root)).resolve()
    if target.is_relative_to(root.resolve()):
        raise CliError("архив нельзя класть внутрь папки проекта: укажите другое место в --out")
    if target.is_dir():
        raise UsageError(f"--out — это папка: укажите имя файла, например {target / (root.name + '.zip')}")
    files, reasons, scanner = verify(root)
    if reasons:
        raise CliError("проект не упакован: найдено то, чего в архиве быть не должно", reasons)
    try:
        count = build_archive(root, files, target)
    except OSError as exc:
        raise CliError(f"архив не записан в {target}: {exc.strerror or exc}") from exc
    return Packed(target, count, target.stat().st_size, scanner)
