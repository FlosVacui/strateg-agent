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

from strateg import calcrun, units
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


def integrity_problems(root: Path, files: dict[str, str] | None, events: list[Any]) -> list[str]:
    """Ошибки `check`: `outputs/calc/<имя>.json` правили не командой `calc run`.

    Хеш файла сверяется с `state.yaml` (`files`); правку, которую уже «приняла» другая команда (событие
    «изменено вне агента» после последнего `calc run`), журнал помнит.
    """
    if files is None:
        return []
    errors: list[str] = []
    for name in result_names(root):
        rel = f"{CALC_DIR}/{name}.json"
        again = f"пересчитайте: `strateg calc run {CALC_DIR}/{name}.py`"
        recorded = files.get(rel)
        if recorded is None:
            errors.append(f"расчёт {name}: {rel} нет в учёте CLI: записан не командой calc run; {again}")
        elif _sha((root / rel).read_bytes()) != recorded:
            errors.append(f"расчёт {name}: файл {rel} изменён вне CLI (хеш не совпал с state.yaml); {again}")
        else:
            run_at = outside_at = -1
            for n, e in enumerate(events):
                if e.kind == "изменено вне агента" and e.path == rel:
                    outside_at = n
                elif e.name == "strateg calc run" and (e.message or "").startswith(f"{name}: "):
                    run_at = n
            if outside_at > run_at:
                errors.append(f"расчёт {name}: файл {rel} правили вне CLI после последнего calc run; {again}")
    return errors


# --- число утверждения и значение расчёта ----------------------------------------------------------------

_NUM = re.compile(
    r"(?<![\w.,])([-−+]?(?:\d{1,3}(?:,\d{3})+\.\d+|\d{1,3}(?:,\d{3}){2,}"
    r"|(?:\d{1,3}(?:[    ]\d{3})+|\d+)(?:[.,]\d+)?))(?!\w)"
)
_DECLINE = re.compile(r"сниж\w*|паден\w*|пад\w*|упал\w*|сокращ\w*|уменьш\w*|спад\w*|минус", re.I)
_GROWTH = re.compile(r"рост\w*|прирост\w*|увеличен\w*|вырос\w*|выросл\w*|повышен\w*|плюс", re.I)
_CLAUSE_END = re.compile(r"[;:!?" + chr(10) + r"]|\.\s")
_YEAR_CONTEXT = re.compile(r"^\s*(?:г\.|г(?!\w)|год\w*)|^\s*(?:-?й\s*)?(?:кв\.?|квартал|месяц|мес\.)", re.I)
_PERCENT_SCALE_FLOOR = Decimal(1)  # меньше единицы по модулю — доля («0,153»), а не проценты


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


def _sign_hint(raw: str, before: str) -> int | None:
    """Знак числа в тексте: явный «−», «+» или слово («снижение», «рост») в той же фразе; иначе None."""
    if raw[0] in "-−":
        return -1
    if raw[0] == "+":
        return 1
    clause = _CLAUSE_END.split(before[-40:])[-1]
    last = max(
        ((m.start(), -1) for m in _DECLINE.finditer(clause)),
        default=(-1, 0),
    )
    grow = max(((m.start(), 1) for m in _GROWTH.finditer(clause)), default=(-1, 0))
    best = max(last, grow)
    return best[1] or None


def matching_numbers(text: str, value: float) -> list[str]:
    """Числа текста (без пробелов-разрядов, с точкой), равные значению с точностью до показанных знаков.

    Единица после числа задаёт согласованное значение, запасного «как есть» нет: «12,4 млн» — 12 400 000,
    «15,3 %» — 0,153 (или 15,3 при |значение| ≥ 1), «5 тыс.» не равно 5. Знак: явный «−»/«+» или слово
    («снижение», «рост») должен совпасть со знаком значения; без знака сверяется модуль. Год («2024 г.»),
    номер квартала и месяца не считаются. Показанные знаки округляются, но ненулевое значение не
    округляется до нуля.
    """
    try:
        exact = Decimal(repr(float(value)))
    except (InvalidOperation, ValueError, OverflowError):
        return []
    found = []
    for m in _NUM.finditer(text):
        raw = m.group(1).replace("−", "-")
        tail = text[m.end() : m.end() + 16]
        if _YEAR_CONTEXT.match(tail):
            continue
        hint = _sign_hint(raw, text[: m.start()])
        if hint is not None and exact != 0 and (hint < 0) != (exact < 0):
            continue
        clean = raw.lstrip("+-")
        for ch in "    ":
            clean = clean.replace(ch, "")
        if "," in clean and ("." in clean or clean.count(",") > 1):
            clean = clean.replace(",", "")
        clean = clean.replace(",", ".")
        try:
            shown = Decimal(clean)
        except InvalidOperation:
            continue
        power = units.scale_after(tail)
        if power is not None:
            candidates = [exact / Decimal(10) ** power]
        elif units.percent_after(tail):
            candidates = [exact * 100] + ([exact] if exact.copy_abs() >= _PERCENT_SCALE_FLOOR else [])
        else:
            candidates = [exact]
        step = Decimal(1).scaleb(-max(-shown.as_tuple().exponent, 0))  # type: ignore[operator]
        try:
            hit = any(
                (shown != 0 or c == 0) and c.copy_abs().quantize(step, ROUND_HALF_UP) == shown
                for c in candidates
            )
        except InvalidOperation:
            continue
        if hit:
            found.append(raw.replace(" ", "").replace(",", "."))
    return found


def number_matches(text: str, value: float) -> bool:
    return bool(matching_numbers(text, value))


def calc_value(root: Path, ref: Any) -> tuple[bool, Any]:
    """Значение расчёта по ссылке утверждения; (False, None) — расчёта или ключа нет."""
    res, _ = load_result(root, ref.name)
    return (False, None) if res is None else value_at(res.values, ref.key)


def calc_has_inputs(root: Path, ref: Any) -> bool:
    res, _ = load_result(root, ref.name)
    return bool(res and res.inputs)


def claim_problems(root: Path, claim: Claim, sources: SourcesFile) -> list[str]:
    """Ошибки действующего утверждения со ссылкой на расчёт: нет расчёта или ключа, число или входы не те.

    Отозванные и заменённые утверждения — история: с расчётом они не сверяются.
    """
    ref = claim.calc
    if ref is None or claim.status != "действует":
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
            "совпадать с расчётом с точностью до показанных знаков и знаком; «тыс.», «млн», «млрд» — это "
            "единицы (12,4 млн — 12400000), «%» — доля (15,3 % — 0.153)"
        )
    by_id = {s.id: s for s in sources.sources}
    cited = {q.source for q in claim.quotes if q.check is not None}
    if claim.type == "факт" and not res.inputs:
        problems.append(
            f"{where}: факт по расчёту без входов: расчёт ничего не прочитал из источников через "
            "strateg_calc.table или strateg_calc.text; запишите утверждение как допущение или прогноз"
        )
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


def _unusable(root: Path, s: Source) -> str | None:
    """Почему расчёт не может читать источник; None — может."""
    if s.kind == "синтетика":
        return "синтетика: расчёт на ней ничего не доказывает"
    if s.kind == "изображение":
        return "изображение: у него нет текста таблицы"
    if not s.text_path or not s.text_sha256:
        return "у источника нет текста"
    if (s.anonymized and not s.anonymization_confirmed) or (s.closed_markers and not s.closed_confirmed):
        return f"человек не подтвердил замены или открытость материала (`strateg add --confirm {s.id}`)"
    path = root / s.text_path
    if not path.is_file() or _sha(path.read_bytes()) != s.text_sha256:
        return "текст источника не совпадает с записанным хешем"
    return None


def execute(root: Path, sources: SourcesFile, name: str, timeout: int, *, guard: bool = True) -> Run:
    """Запускает расчёт отдельным процессом; отказ — `CliError` с причиной.

    `guard=False` снимает разбор скрипта (только тесты хука аудита; из командной строки это недоступно).
    """
    from strateg.project import CliError

    script = root / CALC_DIR / f"{name}.py"
    if guard:
        try:
            problems = calcrun.check_script(script.read_bytes())
        except calcrun.ScriptSyntaxError as exc:
            raise CliError(f"расчёт {name}: {exc}") from None
        if problems:
            raise CliError(
                f"расчёт {name} отклонён до запуска: скрипт нарушает правила ({len(problems)}); "
                "в расчёте разрешены math, statistics, decimal, fractions, datetime, json, itertools, "
                "collections, re и strateg_calc",
                problems[:6],
            )
    why = {s.id: _unusable(root, s) for s in sources.sources}
    spec = {
        "root": str(root),
        "script": f"{CALC_DIR}/{name}.py",
        "guard": guard,
        "sources": {
            s.id: {"path": s.text_path, "sha256": s.text_sha256} for s in sources.sources if why[s.id] is None
        },
        "refused": {sid: reason for sid, reason in why.items() if reason},
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
    drifted = [
        c.id
        for c in p.claims.claims
        if c.calc is not None
        and c.calc.name == name
        and c.status == "действует"
        and any("в тексте нет числа" in x for x in claim_problems(p.root, c, p.sources))
    ]
    lines = [f"Расчёт {name}: записан {CALC_DIR}/{name}.json."]
    lines += [f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in list(run.values.items())[:20]]
    lines.append("Входы: " + (", ".join(sorted(run.inputs)) or "нет"))
    if drifted:
        lines.append(
            f"Внимание: утверждения {', '.join(drifted)} перестали совпадать с расчётом: `strateg check` "
            "назовёт ошибку; замените утверждение (`claim replace`) или отзовите (`claim revoke`)."
        )
    return emit(
        args,
        {
            "name": name,
            "path": f"{CALC_DIR}/{name}.json",
            "values": run.values,
            "inputs": run.inputs,
            "drifted_claims": drifted,
        },
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
