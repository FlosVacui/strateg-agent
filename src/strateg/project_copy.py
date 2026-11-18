"""Копия проекта: `strateg new <id> --from <проект>` (ADR-0003, сценарий С-12).

Копия — обычный проект пользователя, поэтому всё, что ему нужно, строится заново, а источник не меняется
ни на байт: он открывается без блокировки (`Project.load_readonly`), `.tmp/lock` в нём не создаётся.

Что переносится: файлы проекта (как в архиве `pack`), без `.tmp/` (блокировка, исходники материалов до
подтверждения замен, смещения учёта), `.cache/` и временных файлов. Исходники материалов с персональными
данными лежат вне папки проекта (`~/.strateg/private/<id>/`) и тоже остаются у источника.

Что меняется в копии:
- номер проекта в `state.yaml`; бюджет — как у нового проекта, расход с нуля;
- `cost.jsonl` начинается пустым: прежние записи — деньги, потраченные на источник, и они остаются в нём;
- журнал: прежние события не трогаются, в конец пишется событие «копия проекта» (откуда, шаг и ворота
  источника, его прежний расход); время работы копии считается от этого события (`project.own_events`);
- хеши файлов в `state.yaml` пересчитываются, поэтому `check` копии чист, а «изменено вне агента» не
  возникает; расхождения, которые уже были в источнике, записываются обычным событием.

Копия строится во временной папке рядом под её блокировкой и переименовывается в `projects/<номер>/` в конце;
при любой ошибке временная папка убирается целиком.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from strateg import pack as packlib
from strateg import project as proj
from strateg.budget import load_defaults
from strateg.project import CliError, Project, UsageError
from strateg.project_check import COST_FILE, check_project, key_hash

NOT_COPIED = (".tmp/", ".cache/")
CHANGED_TEXT = "источник менялся во время копирования, копия не создана: повторите команду"


@dataclass
class CopyResult:
    project: Project
    source: str
    source_step: str
    files: int
    mentions: list[str] = field(default_factory=list)  # файлы копии, где упомянута папка источника
    skipped: list[str] = field(default_factory=list)  # что из `NOT_COPIED` было в источнике и не вошло


def resolve_source(name: str) -> Path:
    """Папка источника: номер проекта (`projects/<номер>/`) или путь к папке."""
    looks_like_path = any(sep in name for sep in ("/", "\\")) or Path(name).is_absolute()
    root = Path(name) if looks_like_path else proj.projects_root() / proj.check_project_id(name)
    if not (root / proj.STATE_FILE).is_file():
        raise CliError(
            f"проект-источник не найден: {name} (нет файла {proj.STATE_FILE}). "
            "Список проектов — папка `projects/`; пример лежит в `projects/example/`"
        )
    return root


def _mentions(root: Path, files: list[Path], source_name: str) -> list[str]:
    needle = f"projects/{source_name}/"
    found = []
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text:
            found.append(path.relative_to(root).as_posix())
    return found


def _stop_text(src: Project, step_name: str) -> str:
    gates = ", ".join(f"{g} {s}" for g, s in src.state.gates.items())
    return f"остановка источника: шаг {src.state.step} ({step_name}); ворота: {gates}"


def _earlier_work(src: Project) -> str:
    minutes = proj.work_seconds(src.journal) / 60
    total, precision, _ = proj.cost_total(src.costs)
    money = "стоимость неизвестна" if total is None else f"${total:.2f} ({precision})".replace(".", ",")
    return f"{minutes:.1f}".replace(".", ",") + f" мин работы, {money}"


def _digest(root: Path, files: list[Path]) -> dict[str, str] | None:
    """Хеши файлов источника; None — какой-то файл исчез или не читается (источник меняют)."""
    try:
        return {p.relative_to(root).as_posix(): proj.sha256_bytes(p.read_bytes()) for p in files}
    except OSError:
        return None


def _source_changed(src_root: Path, before: dict[str, str] | None) -> bool:
    """Менялся ли источник с начала копирования: набор файлов или их содержимое."""
    after = _digest(src_root, packlib.project_files(src_root))
    return before is None or after is None or after != before


def _skipped(src_root: Path) -> list[str]:
    """Что из `NOT_COPIED` в источнике было и в копию не вошло."""
    return [
        name
        for name in NOT_COPIED
        if (src_root / name.rstrip("/")).is_dir()
        and any(p.is_file() for p in (src_root / name.rstrip("/")).rglob("*"))
    ]


def copy_project(source: str, new_id: str) -> CopyResult:
    """Копирует проект-источник в `projects/<new_id>/`. Отказ — `CliError`; источник не меняется.

    Копия собирается во временной папке рядом (`projects/.<номер>-….copy`) и одним переименованием встаёт
    на место `projects/<номер>/`: если процесс убит посреди копирования, полупапки с номером не остаётся.
    """
    from strateg.commands import STEP_NAMES  # импорт здесь: commands импортирует этот модуль

    proj.check_project_id(new_id)
    src_root = resolve_source(source)
    dest = proj.projects_root() / new_id
    if dest.exists():
        raise CliError(f"проект {new_id} уже существует: {dest.name}")
    try:
        same = dest.resolve() == src_root.resolve()
    except OSError:
        same = False
    if same:
        raise UsageError("копия не может лежать в папке источника: возьмите другой номер")

    errors = check_project(src_root)
    if errors:
        raise CliError(
            f"проект {src_root.name} не проходит проверку, копировать его нельзя: "
            "исправьте ошибки в источнике",
            errors,
        )
    src = Project.load_readonly(src_root)
    files = packlib.project_files(src_root)
    before = _digest(src_root, files)
    skipped = _skipped(src_root)

    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{new_id}-", suffix=".copy", dir=dest.parent))
    failure: BaseException | None = None
    result: CopyResult | None = None
    try:
        with proj.locked(work):
            for path in files:
                target = work / path.relative_to(src_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            for name in proj.SUBFOLDERS:  # git пустых папок не хранит
                (work / name).mkdir(exist_ok=True)
            proj.write_text(work / COST_FILE, "")
            result = _finish(work, src, src_root, files, new_id, STEP_NAMES)
    except BaseException as exc:  # noqa: BLE001 - папка убирается при любой причине, ошибка идёт дальше
        failure = exc
    if failure is None and result is not None:
        try:
            if _source_changed(src_root, before):
                failure = CliError(CHANGED_TEXT)
            else:
                _put_in_place(work, dest, new_id)
        except BaseException as exc:  # noqa: BLE001
            failure = exc
    if failure is not None or result is None:
        shutil.rmtree(work, ignore_errors=True)  # блокировка отпущена: на Windows занятый файл не удалить
        if (
            isinstance(failure, CliError)
            and failure.message != CHANGED_TEXT
            and _source_changed(src_root, before)
        ):
            raise CliError(CHANGED_TEXT) from failure  # копия не прошла проверку из-за правки источника
        if failure is not None:
            raise failure
        raise CliError("копия не создана")
    proj.set_active(new_id)
    result.project = Project.load_readonly(dest)
    result.skipped = skipped
    return result


def _put_in_place(work: Path, dest: Path, new_id: str) -> None:
    """Переименовывает готовую копию в `projects/<номер>/`; занятый номер — отказ."""
    for attempt in range(5):
        if dest.exists():
            raise CliError(f"проект {new_id} уже существует: {dest.name}")
        try:
            work.rename(dest)
            return
        except FileExistsError:
            raise CliError(f"проект {new_id} уже существует: {dest.name}") from None
        except PermissionError:  # Windows: антивирус или индексатор держат файл, пока не отпустит
            if attempt == 4:
                raise
            time.sleep(0.2)


def _finish(
    dest: Path, src: Project, src_root: Path, files: list[Path], new_id: str, step_names: dict[str, str]
) -> CopyResult:
    p = Project.load_readonly(dest)  # папка уже под блокировкой этого процесса
    p.outside = list(src.outside)  # то, что было не так уже в источнике; cost.jsonl — наша правка
    p.state.id = new_id
    defaults = load_defaults().budget
    p.state.budget.limit_usd = defaults.usd
    p.state.budget.limit_minutes = defaults.minutes
    p.state.budget.spent_usd = None
    p.state.budget.spent_minutes = 0
    p.costs = []
    p.record_outside_changes()
    message = (
        f"копия проекта «{src_root.name}»; {_stop_text(src, step_names[src.state.step])}; "
        f"прежний расход ({_earlier_work(src)}) — работа над источником, в учёт копии не входит"
    )
    p.add_event(
        p.event(
            kind="действие",
            name=proj.COPY_ACTION,
            idempotency_hash=key_hash(
                "copy", src_root.name, new_id, proj.sha256_bytes(_journal_bytes(src_root))
            ),
            status="выполнено",
            duration_s=0,
            message=message,
        )
    )
    p.state.budget.spent_minutes = round(proj.work_seconds(p.journal) / 60, 2)
    p.state.budget.spent_usd = proj.cost_total(p.costs)[0]
    p.save_state()
    errors = check_project(dest)
    if errors:
        raise CliError("копия не прошла проверку, она не создана", errors)
    left = Project.load_readonly(dest).outside
    if left:
        raise CliError(
            "копия не прошла проверку хешей файлов, она не создана", [f"{r}: {w}" for r, w in left]
        )
    return CopyResult(
        project=p,
        source=src_root.name,
        source_step=src.state.step,
        files=len(files),
        mentions=_mentions(dest, [dest / f.relative_to(src_root) for f in files], src_root.name),
    )


def _journal_bytes(root: Path) -> bytes:
    path = root / proj.JOURNAL_FILE
    return path.read_bytes() if path.is_file() else b""
