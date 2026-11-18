"""Воспроизводимые расчёты: `strateg calc run`, `strateg calc check` и связь числа утверждения с расчётом.

Скрипт лежит в `outputs/calc/<имя>.py`, читает источники проекта по номеру `И-n` и печатает JSON. CLI
запускает его отдельным процессом (`strateg.calcrun`) и пишет `outputs/calc/<имя>.json`: значения, хеши
скрипта и входов, время, версию Python (`CalcResult`). `check` проекта сверяет хеши, `calc check` считает
заново и сравнивает значения. Утверждение ссылается на расчёт полем `calc: {name, key}` (ADR-0005).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from strateg import calcrun
from strateg.models import SCHEMA_VERSION, CalcResult, Claim, Source, SourcesFile

CALC_DIR = "outputs/calc"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
_NAME = re.compile(r"^[a-zа-яё0-9][a-zа-яё0-9_-]*$")
_ENV_KEEP = ("SYSTEMROOT", "SystemDrive", "windir")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def result_names(root: Path) -> list[str]:
    folder = root / CALC_DIR
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if p.is_file())


def script_names(root: Path) -> list[str]:
    folder = root / CALC_DIR
    return sorted(p.stem for p in folder.glob("*.py") if p.is_file()) if folder.is_dir() else []


def load_result(root: Path, name: str) -> tuple[CalcResult | None, str | None]:
    path = root / CALC_DIR / f"{name}.json"
    try:
        return CalcResult.model_validate(json.loads(path.read_bytes().decode("utf-8"))), None
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        why = "не по схеме" if isinstance(exc, ValidationError) else "не читается"
        return (
            None,
            f"{CALC_DIR}/{name}.json: файл {why}; пересчитайте `strateg calc run {CALC_DIR}/{name}.py`",
        )


def stale_problems(root: Path, sources: SourcesFile | None) -> list[str]:
    """Ошибки `check`: результат не читается, скрипт или вход изменились после записи результата."""
    errors: list[str] = []
    by_id = {s.id: s for s in sources.sources} if sources else {}
    for name in result_names(root):
        res, err = load_result(root, name)
        label = f"расчёт {name}"
        if res is None:
            errors.append(err or label)
            continue
        again = f"пересчитайте: `strateg calc run {CALC_DIR}/{name}.py`"
        script = root / res.script.path
        if not script.is_file():
            errors.append(f"{label}: скрипта {res.script.path} нет, а результат есть")
        elif _sha(script.read_bytes()) != res.script.sha256:
            errors.append(f"{label}: скрипт {res.script.path} изменён после записи результата; {again}")
        for inp in res.inputs:
            src = by_id.get(inp.source)
            if sources is not None and src is None:
                errors.append(f"{label}: вход {inp.source} исчез из реестра источников")
            elif src is not None and src.text_sha256 != inp.sha256:
                errors.append(f"{label}: вход {inp.source} изменился после расчёта; {again}")
    return errors


# --- число утверждения и значение расчёта ----------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.,])([-−+]?(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d+)?)(?!\w)")
_SCALES = ((r"^\s*млрд", 9), (r"^\s*млн", 6), (r"^\s*тыс", 3))


def value_at(values: Any, key: str) -> tuple[bool, Any]:
    """Значение по пути вида `итог` или `по_годам.2024`; ключ с точкой внутри тоже находится."""
    node = values
    rest = key
    while True:
        if isinstance(node, dict):
            if rest in node:
                return True, node[rest]
            head, sep, tail = rest.partition(".")
            while sep:
                if head in node:
                    break
                more, sep2, tail2 = tail.partition(".")
                head, tail, sep = f"{head}.{more}", tail2, sep2
            if head in node and sep:
                node, rest = node[head], tail
                continue
            return False, None
        if isinstance(node, list):
            head, _, tail = rest.partition(".")
            if head.isdigit() and int(head) < len(node):
                node = node[int(head)]
                if not tail:
                    return True, node
                rest = tail
                continue
        return False, None


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def matching_numbers(text: str, value: float) -> list[str]:
    """Числа текста (без пробелов-разрядов, с точкой), равные значению с точностью до показанных знаков.

    Принимаются «15,3 %» при значении 0,153, «12,4 млн» при 12 400 000 и знак минус словом («снижение на 5»).
    """
    try:
        exact = Decimal(repr(float(value)))
    except InvalidOperation:
        return []
    found = []
    for m in _NUM.finditer(text):
        raw = m.group(1).replace("−", "-")
        for ch in "   ":
            raw = raw.replace(ch, "")
        raw = raw.replace(",", ".")
        try:
            shown = Decimal(raw)
        except InvalidOperation:
            continue
        digits = max(-shown.as_tuple().exponent, 0)  # type: ignore[operator]
        tail = text[m.end() : m.end() + 14]
        candidates = [exact]
        if re.match(r"^\s*%", tail):
            candidates.append(exact * 100)
        for pattern, power in _SCALES:
            if re.match(pattern, tail, re.IGNORECASE):
                candidates.append(exact / Decimal(10) ** power)
        step = Decimal(1).scaleb(-digits)
        if any(c.copy_abs().quantize(step, ROUND_HALF_UP) == shown.copy_abs() for c in candidates):
            found.append(raw)
    return found


def number_matches(text: str, value: float) -> bool:
    return bool(matching_numbers(text, value))


def calc_value(root: Path, ref: Any) -> tuple[bool, Any]:
    """Значение расчёта по ссылке утверждения; (False, None) — расчёта или ключа нет."""
    res, _ = load_result(root, ref.name)
    return (False, None) if res is None else value_at(res.values, ref.key)


def claim_problems(root: Path, claim: Claim, sources: SourcesFile) -> list[str]:
    """Ошибки утверждения со ссылкой на расчёт: нет расчёта или ключа, число не совпало, входы не те."""
    ref = claim.calc
    if ref is None:
        return []
    where = f"поле calc ({ref.name}, {ref.key})"
    res, err = load_result(root, ref.name)
    if res is None:
        if not (root / CALC_DIR / f"{ref.name}.json").is_file():
            return [f"{where}: расчёта {ref.name} нет; запустите `strateg calc run {CALC_DIR}/{ref.name}.py`"]
        return [f"{where}: {err}"]
    found, value = value_at(res.values, ref.key)
    if not found:
        return [f"{where}: в результате расчёта {ref.name} нет ключа «{ref.key}»"]
    if not is_number(value):
        return [f"{where}: значение «{ref.key}» в расчёте {ref.name} не число"]
    problems = []
    if not number_matches(claim.text, value):
        problems.append(
            f"{where}: в тексте нет числа, равного значению расчёта ({value}); число утверждения должно "
            "совпадать с расчётом с точностью до показанных знаков"
        )
    by_id = {s.id: s for s in sources.sources}
    cited = {q.source for q in claim.quotes if q.check is not None}
    for inp in res.inputs:
        src = by_id.get(inp.source)
        if src is not None and src.kind == "синтетика":
            problems.append(f"{where}: вход {inp.source} — синтетика, расчёт на ней ничего не доказывает")
        elif claim.type == "факт" and inp.source not in cited:
            problems.append(
                f"{where}: факт по расчёту, а вход {inp.source} не процитирован: добавьте цитату из него "
                "или запишите утверждение как допущение или прогноз"
            )
    return problems


# --- запуск ------------------------------------------------------------------------------------------------


@dataclass
class Run:
    values: dict[str, Any]
    inputs: dict[str, str]  # источник -> sha256 текста


def script_name(root: Path, raw: str) -> str:
    """Имя расчёта по пути скрипта; скрипт — только `outputs/calc/<имя>.py` внутри проекта."""
    from strateg.project import CliError, UsageError

    given = Path(raw)
    candidates = [given] if given.is_absolute() else [Path.cwd() / given, root / given]
    folder = (root / CALC_DIR).resolve()
    for c in candidates:
        try:
            full = c.resolve()
        except OSError:
            continue
        if full.is_file() and full.parent == folder and full.suffix == ".py":
            if not _NAME.match(full.stem):
                raise UsageError(
                    f"имя расчёта «{full.stem}» не подходит: строчные буквы, цифры, «_» и «-», без пробелов"
                )
            return full.stem
    if any(c.is_file() for c in candidates):
        raise CliError(f"скрипт расчёта лежит только в {CALC_DIR}/<имя>.py, а не в «{raw}»")
    raise CliError(f"скрипт {raw} не найден: положите его в {CALC_DIR}/<имя>.py проекта")


def _usable(root: Path, s: Source) -> bool:
    if not s.text_path or not s.text_sha256 or s.kind in ("изображение", "синтетика"):
        return False
    if (s.anonymized and not s.anonymization_confirmed) or (s.closed_markers and not s.closed_confirmed):
        return False
    path = root / s.text_path
    return path.is_file() and _sha(path.read_bytes()) == s.text_sha256


def execute(root: Path, sources: SourcesFile, name: str, timeout: int) -> Run:
    """Запускает расчёт отдельным процессом; отказ — `CliError` с причиной."""
    from strateg.project import CliError

    spec = {
        "root": str(root),
        "script": f"{CALC_DIR}/{name}.py",
        "sources": {
            s.id: {"path": s.text_path, "sha256": s.text_sha256} for s in sources.sources if _usable(root, s)
        },
    }
    env = {
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        **{k: os.environ[k] for k in _ENV_KEEP if k in os.environ},
    }
    cmd = [sys.executable, "-s", "-B", str(Path(calcrun.__file__).resolve())]
    try:
        r = subprocess.run(
            cmd,
            input=json.dumps(spec),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CliError(
            f"расчёт {name} не уложился в {timeout} с и остановлен: упростите расчёт "
            f"(предел задаёт --timeout, не больше {MAX_TIMEOUT})"
        ) from None
    if r.returncode != 0:
        raise CliError(f"расчёт {name} не запустился (код {r.returncode})", [r.stderr.strip()[-600:]])
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise CliError(f"расчёт {name}: служебный ответ процесса не читается") from None
    if out["violations"]:
        raise CliError(
            f"расчёт {name} отклонён: скрипт обращался к закрытому ({len(out['violations'])})",
            [str(v) for v in out["violations"][:5]],
        )
    if out["status"] != 0:
        tail = [line for line in out["stderr"].strip().splitlines() if line][-6:]
        raise CliError(f"расчёт {name} завершился ошибкой", tail)
    try:
        values = json.loads(out["stdout"])
    except ValueError:
        raise CliError(
            f"расчёт {name}: скрипт должен напечатать один JSON-объект (удобнее всего "
            "`strateg_calc.emit({...})`), печатать больше ничего не нужно"
        ) from None
    if not isinstance(values, dict) or not values:
        raise CliError(f"расчёт {name}: результат — непустой JSON-объект «ключ: значение»")
    return Run(values, dict(out["used"]))


def build_result(
    root: Path, name: str, run: Run, when: Any, py: str, paths: dict[str, str | None]
) -> CalcResult:
    script = f"{CALC_DIR}/{name}.py"
    inputs = [
        {"source": sid, "path": paths[sid], "sha256": run.inputs[sid]}
        for sid in sorted(run.inputs, key=lambda x: int(x.split("-")[1]))
    ]
    return CalcResult.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "name": name,
            "script": {"path": script, "sha256": _sha((root / script).read_bytes())},
            "inputs": inputs,
            "values": run.values,
            "run_at": when,
            "python": py,
        }
    )


# --- команды -----------------------------------------------------------------------------------------------


def _timeout(args: argparse.Namespace) -> int:
    from strateg.project import UsageError

    t = getattr(args, "timeout", None) or DEFAULT_TIMEOUT
    if not 1 <= t <= MAX_TIMEOUT:
        raise UsageError(f"--timeout от 1 до {MAX_TIMEOUT} секунд")
    return int(t)


def cmd_calc_run(args: argparse.Namespace) -> int:
    from strateg import project as proj
    from strateg.commands import _open, emit
    from strateg.project import CliError

    p = _open(args)
    name = script_name(p.root, args.script)
    started = time.monotonic()
    run = execute(p.root, p.sources, name, _timeout(args))  # долгая работа — до блокировки папки
    py = ".".join(map(str, sys.version_info[:3]))
    # утверждение, чьё число расчёт теперь не подтверждает, — ошибка `check`, а не отказ записать расчёт
    with proj.transaction(p, tolerate=lambda e: "поле calc (" in e):
        by_id = {s.id: s for s in p.sources.sources}
        for sid, sha in run.inputs.items():
            s = by_id.get(sid)
            if s is None or s.text_sha256 != sha:
                raise CliError(f"источник {sid} изменился во время расчёта: повторите команду")
        res = build_result(p.root, name, run, proj.now(), py, {s.id: s.text_path for s in p.sources.sources})
        data = res.model_dump(mode="json", exclude_none=True)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        proj.write_text(p.root / CALC_DIR / f"{name}.json", text)
        p.add_event(
            p.event(
                kind="действие",
                name="strateg calc run",
                idempotency_hash=proj.key_hash("calc run", name, _sha(text.encode("utf-8")), len(p.journal)),
                status="выполнено",
                duration_s=round(time.monotonic() - started, 2),
                message=f"{name}: значений {len(run.values)}, входов {len(run.inputs)}",
            )
        )
        p.save_state()
    lines = [f"Расчёт {name}: записан {CALC_DIR}/{name}.json."]
    lines += [f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in list(run.values.items())[:20]]
    lines.append("Входы: " + (", ".join(sorted(run.inputs)) or "нет"))
    return emit(
        args,
        {"name": name, "path": f"{CALC_DIR}/{name}.json", "values": run.values, "inputs": run.inputs},
        lines,
    )


def cmd_calc_check(args: argparse.Namespace) -> int:
    from strateg.commands import _open, emit
    from strateg.project import CliError

    p = _open(args)
    timeout = _timeout(args)
    problems: list[str] = []
    checked = []
    for name in result_names(p.root):
        res, err = load_result(p.root, name)
        if res is None:
            problems.append(f"расчёт {name}: {err}")
            continue
        script = p.root / res.script.path
        if not script.is_file():
            problems.append(f"расчёт {name}: скрипта {res.script.path} нет")
            continue
        if _sha(script.read_bytes()) != res.script.sha256:
            problems.append(f"расчёт {name}: скрипт изменён после записи результата")
        try:
            run = execute(p.root, p.sources, name, timeout)
        except CliError as exc:
            problems.append(
                f"расчёт {name}: " + exc.message + (f" ({exc.details[0]})" if exc.details else "")
            )
            continue
        saved = {i.source: i.sha256 for i in res.inputs}
        if run.inputs != saved:
            problems.append(f"расчёт {name}: изменился состав или текст входов")
        if run.values != res.values:
            problems.append(f"расчёт {name}: значения при повторе не совпали с сохранёнными")
        checked.append(name)
    notes = [
        f"расчёт {n}: результата нет, скрипт ни разу не запускали (`strateg calc run {CALC_DIR}/{n}.py`)"
        for n in script_names(p.root)
        if n not in result_names(p.root)
    ]
    if problems:
        raise CliError(f"проверка расчётов: ошибок {len(problems)}", problems)
    return emit(
        args,
        {"checked": checked, "notes": notes},
        [f"Расчётов проверено: {len(checked)}, повтор даёт то же."] + [f"Внимание: {n}" for n in notes],
    )
