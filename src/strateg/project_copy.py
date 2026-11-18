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

Копия строится в новой папке под её блокировкой; при любой ошибке папка убирается целиком.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from strateg import pack as packlib
from strateg import project as proj
from strateg.budget import load_defaults
from strateg.project import CliError, Project, UsageError
from strateg.project_check import COST_FILE, check_project, key_hash

NOT_COPIED = (".tmp/", ".cache/")


@dataclass
class CopyResult:
    project: Project
    source: str
    source_step: str
    files: int
    mentions: list[str] = field(default_factory=list)  # файлы копии, где упомянута папка источника


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


def copy_project(source: str, new_id: str) -> CopyResult:
    """Копирует проект-источник в `projects/<new_id>/`. Отказ — `CliError`; источник не меняется."""
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

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir()  # занимает номер: параллельная копия с тем же номером получит отказ
    except FileExistsError:
        raise CliError(f"проект {new_id} уже существует: {dest.name}") from None

    failure: BaseException | None = None
    result: CopyResult | None = None
    try:
        with proj.locked(dest):
            for path in files:
                target = dest / path.relative_to(src_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            for name in proj.SUBFOLDERS:  # git пустых папок не хранит
                (dest / name).mkdir(exist_ok=True)
            proj.write_text(dest / COST_FILE, "")
            result = _finish(dest, src, src_root, files, new_id, STEP_NAMES)
    except BaseException as exc:  # noqa: BLE001 - папка убирается при любой причине, ошибка идёт дальше
        failure = exc
    if failure is not None or result is None:
        shutil.rmtree(dest, ignore_errors=True)  # блокировка уже отпущена: на Windows занятый файл не удалить
        if failure is not None:
            raise failure
        raise CliError("копия не создана")
    proj.set_active(new_id)
    return result


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
