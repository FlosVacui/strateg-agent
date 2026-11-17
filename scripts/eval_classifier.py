"""Замер классификатора (ADR-0007, ADR-0008): связка «дешёвая + повтор „не уверен“ на средней» против Sonnet.

Выборки: основная `evals/classifier/pairs.yaml` и контрольная `evals/classifier/holdout.yaml` (её составляли
без знакомства с текстом роли). Задание собирает тот же код, что и `strateg claim pairs`
(`strateg.classify.render_task`: только пары); текст роли — системный промпт вызова (`--system-prompt-file`,
тот же текст, что тело обёртки `.claude/agents/classifier.md`); ответ разбирает тот же строгий разбор, что и
`strateg claim verdict --file`: неразобранная пара — «не уверен». Все пары выборки по умолчанию идут одним
заданием, как в продукте.

Решение ADR-0007 — о связке, как роль работает в продукте: дешёвая модель на всех парах, пары «не уверен»
(и неразобранные) — повтор на средней модели одним заданием. По каждой выборке: точность связки не ниже 90 %,
ложных «подтверждает» не больше одного, цена пары ниже Sonnet — «оставить», иначе «убрать». Решение — по
обеим выборкам. Одиночные модели — справочно. Сбой вызова (код выхода, `is_error`, тайм-аут) — пачка
«сбой вызова», замер останавливается, решения нет.

Замер (нужен вход в claude; вызов — `claude -p` без инструментов и MCP, вне репозитория):
    uv run --frozen python scripts/eval_classifier.py --save evals/classifier/results
Проверка разбора без сети и без claude, на заготовленных ответах `evals/classifier/dry-run.yaml`:
    uv run python scripts/eval_classifier.py --dry-run

Коды выхода: 0 — решение вынесено, 1 — решения нет (сбой вызова, нет контрольной выборки, нет цены),
2 — `claude` не найден или вход не по формату.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from strateg import classify
from strateg.instructions import find_signs

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "evals" / "classifier" / "pairs.yaml"
HOLDOUT = ROOT / "evals" / "classifier" / "holdout.yaml"
DRY = ROOT / "evals" / "classifier" / "dry-run.yaml"
VERDICT_ROWS = ("подтверждает", "частично", "не подтверждает")
GROUPS = ("подтверждает", "не подтверждает", "частично", "неверный тип")
MIN_ACCURACY = (9, 10)  # ADR-0007: не ниже 90 %; сравнение в целых числах — ровно 90 % проходит
MAX_FALSE_SUPPORT = 1  # ложных «подтверждает» на выборку; больше — роль убираем
REFERENCE = "sonnet"
RETRY = "retry"  # повтор «не уверен» на средней модели: ключ ответов в заготовке и приставка модели
TIMEOUT_S = 600
PROMPT_FILE = "role.md"  # системный промпт файлом: многострочный аргумент через `claude.cmd` не пройдёт
FAILED = "сбой вызова"
NO_DECISION = "решения нет"
SERVICE_NOTE = (
    "Цена — по `total_cost_usd` и включает служебный контекст `claude -p` (настройки и память профиля): "
    "у субагента в продукте он свой, цену пары это завышает у обеих моделей."
)
SAMPLES = {"main": "основная", "holdout": "контрольная"}


@dataclass(frozen=True)
class Reply:
    text: str
    cost: float | None
    error: str | None = None


Ask = Callable[[str, str, int, str], Reply]  # выборка, модель (или «retry:<модель>»), номер пачки, задание


class CallFailed(Exception):
    """Сбой вызова: замер останавливается, решение не выносится."""


def load_pairs(path: Path = PAIRS) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    pairs = data["pairs"]
    seen: set[str] = set()
    for p in pairs:
        assert p["id"] not in seen, f"номер {p['id']} повторяется"
        seen.add(p["id"])
        assert p["expected"]["verdict"] in classify.VERDICTS and p["expected"]["verdict"] != "не уверен", p[
            "id"
        ]
        assert p["type"] in classify.TYPES and p["expected"]["type"] in classify.TYPES, p["id"]
        assert p["group"] in GROUPS, p["id"]
        assert p["quotes"], p["id"]
    return pairs


def to_pair(p: dict[str, Any]) -> classify.Pair:
    """Пара как у `claim pairs`: цитата одной строкой; «подозрительный» — тот же детектор, что у источника."""
    quotes = []
    for q in p["quotes"]:
        text, truncated = classify.cut(q["text"])
        quotes.append(classify.PairQuote(q["source"], text, truncated, bool(find_signs(q["text"]))))
    return classify.Pair(
        p["id"], p["type"], classify.one_line(p["text"]), p.get("period"), p.get("region"), tuple(quotes)
    )


def batches(pairs: list[dict[str, Any]], size: int | None) -> list[list[dict[str, Any]]]:
    if not size or size >= len(pairs):
        return [pairs]
    return [pairs[i : i + size] for i in range(0, len(pairs), size)]


def ask_claude(exe: str, model: str, task: str, role: str, effort: str | None = None) -> Reply:
    """Один вызов `claude -p`: без инструментов и MCP, без сохранения сессии, в пустой папке вне проекта."""
    argv = [exe, "-p", "--model", model, "--output-format", "json", "--tools", ""]
    argv += ["--strict-mcp-config", "--no-session-persistence", "--system-prompt-file", PROMPT_FILE]
    if effort:
        argv += ["--effort", effort]
    with tempfile.TemporaryDirectory() as cwd:
        (Path(cwd) / PROMPT_FILE).write_bytes(role.encode("utf-8"))
        try:
            r = subprocess.run(
                argv,
                input=task,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return Reply("", None, f"тайм-аут {TIMEOUT_S} с")
        except OSError as exc:
            return Reply("", None, f"claude не запустился: {exc}")
    if r.returncode != 0:
        return Reply(r.stdout[:2000], None, f"код {r.returncode}: {r.stderr.strip()[:300]}")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return Reply(r.stdout[:2000], None, "ответ не JSON")
    if not isinstance(out, dict):
        return Reply(r.stdout[:2000], None, "ответ не объект JSON")
    raw_cost = out.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
    text = str(out.get("result") or "")
    if out.get("is_error"):
        return Reply(text, cost, f"is_error: {out.get('subtype') or ''} {text[:300]}".strip())
    return Reply(text, cost)


# --- прогон ------------------------------------------------------------------------------------------------


@dataclass
class Run:
    """Замер целиком: сырые ответы, итоги и сбой. С `save` всё пишется после каждой пачки."""

    dry_run: bool
    batch: int | None
    save: Path | None = None
    answers: list[dict[str, Any]] = field(default_factory=list)
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure: str | None = None
    decision: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "batch": self.batch,
            "samples": self.samples,
            "failure": self.failure,
            "decision": self.decision,
        }

    def flush(self) -> None:
        if self.save is None:
            return
        self.save.parent.mkdir(parents=True, exist_ok=True)
        full = {**self.summary(), "answers": self.answers}
        self.save.write_bytes((json.dumps(full, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def ask_pairs(
    run: Run, sample: str, model: str, pairs: list[dict[str, Any]], ask: Ask
) -> tuple[dict[str, classify.Parsed], float | None]:
    """Пары пачками; разбор — как у `claim verdict --file`. Сбой вызова — `CallFailed` после записи пачки."""
    parsed: dict[str, classify.Parsed] = {}
    cost: float | None = 0.0
    for n, chunk in enumerate(batches(pairs, run.batch)):
        ids = [p["id"] for p in chunk]
        reply = ask(sample, model, n, classify.render_task([to_pair(p) for p in chunk]))
        raw: dict[str, Any] = {"sample": sample, "model": model, "batch": n, "ids": ids, "text": reply.text}
        raw |= {"cost_usd": reply.cost, "error": reply.error}
        run.answers.append(raw)
        if reply.error is not None:
            run.failure = (
                f"{SAMPLES.get(sample, sample)} выборка, {model}, пачка {n + 1}: {FAILED} — {reply.error}"
            )
            run.flush()
            raise CallFailed(run.failure)
        quotes = {p["id"]: [q["text"] for q in p["quotes"]] for p in chunk}
        answer = classify.parse_answer(reply.text, ids, quotes)
        parsed.update(answer.items)
        raw["unknown_ids"] = answer.unknown
        cost = None if cost is None or reply.cost is None else cost + reply.cost
        run.flush()
    return parsed, cost


def score(
    pairs: list[dict[str, Any]], parsed: dict[str, classify.Parsed], cost: float | None
) -> dict[str, Any]:
    n = len(pairs)
    matrix = {row: dict.fromkeys(classify.VERDICTS, 0) for row in VERDICT_ROWS}
    verdict_ok = type_ok = 0
    false_support: list[str] = []
    wrong: list[str] = []
    for p in pairs:
        got = parsed[p["id"]]
        want = p["expected"]["verdict"]
        matrix[want][got.verdict] += 1
        if got.verdict == want:
            verdict_ok += 1
        else:
            wrong.append(p["id"])
            if got.verdict == "подтверждает":
                false_support.append(p["id"])
        if got.type_ok is not None and got.type_ok == (p["type"] == p["expected"]["type"]):
            type_ok += 1
    unsure_model = sorted((k for k, v in parsed.items() if v.parsed and v.verdict == "не уверен"), key=_num)
    unparsed = sorted((k for k, v in parsed.items() if not v.parsed), key=_num)
    return {
        "pairs": n,
        "verdict_ok": verdict_ok,
        "verdict_accuracy": round(verdict_ok / n, 4),
        "type_accuracy": round(type_ok / n, 4),
        "unsure_model": unsure_model,  # «не уверен» ответила сама модель
        "unsure_model_share": round(len(unsure_model) / n, 4),
        "unparsed": unparsed,  # «не уверен» записал разбор: строки нет, две разные, строка из цитаты
        "unparsed_share": round(len(unparsed) / n, 4),
        "false_support": false_support,
        "wrong": wrong,
        "matrix": matrix,
        "cost_usd": None if cost is None else round(cost, 6),
        "cost_per_pair_usd": None if cost is None else round(cost / n, 8),
        "verdicts": {k: v.verdict for k, v in parsed.items()},
    }


def _num(cid: str) -> int:
    return int(cid.split("-")[1])


def agreement(a: dict[str, Any], b: dict[str, Any]) -> float:
    same = sum(1 for k, v in a["verdicts"].items() if b["verdicts"].get(k) == v)
    return round(same / len(a["verdicts"]), 4)


def disputed(pairs: list[dict[str, Any]], a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, str]]:
    """Пары, где эталон расходится с обеими моделями: их размечают заново вручную."""
    out = []
    for p in pairs:
        want, x, y = p["expected"]["verdict"], a["verdicts"][p["id"]], b["verdicts"][p["id"]]
        if x != want and y != want:
            out.append({"id": p["id"], "expected": want, "cheap": x, "reference": y})
    return out


def cascade_name(cheap: str) -> str:
    return f"{cheap} → {REFERENCE}"


def measure_sample(run: Run, sample: str, path: Path, cheap: str, ask: Ask) -> None:
    """Одиночные модели и связка на одной выборке; итог — в `run.samples` после каждой модели."""
    pairs = load_pairs(path)
    out: dict[str, Any] = {"file": _shown(path), "pairs": len(pairs), "results": {}}
    run.samples[sample] = out
    parsed: dict[str, dict[str, classify.Parsed]] = {}
    costs: dict[str, float | None] = {}
    for model in dict.fromkeys([cheap, REFERENCE]):
        parsed[model], costs[model] = ask_pairs(run, sample, model, pairs, ask)
        out["results"][model] = score(pairs, parsed[model], costs[model])
        run.flush()
    retry_ids = [k for k, v in parsed[cheap].items() if v.verdict == "не уверен"]
    combined, cost = dict(parsed[cheap]), costs[cheap]
    if retry_ids:  # как в продукте: `claim pairs --json --ids <«не уверен»>` на повышенной модели
        retry = [p for p in pairs if p["id"] in retry_ids]
        again, extra = ask_pairs(run, sample, f"{RETRY}:{REFERENCE}", retry, ask)
        combined.update(again)
        cost = None if cost is None or extra is None else cost + extra
    chain = score(pairs, combined, cost)
    chain["retried"] = sorted(retry_ids, key=_num)
    chain["retried_share"] = round(len(retry_ids) / len(pairs), 4)
    out["results"][cascade_name(cheap)] = chain
    out["agreement"] = agreement(out["results"][cheap], out["results"][REFERENCE])
    out["disputed"] = disputed(pairs, out["results"][cheap], out["results"][REFERENCE])
    out["decision"] = sample_decision(out["results"], cheap)
    run.flush()


def sample_decision(results: dict[str, dict[str, Any]], cheap: str) -> dict[str, Any]:
    """Правило ADR-0007 на одной выборке: связка против Sonnet — точность, ложные «подтверждает», цена."""
    c, ref = results[cascade_name(cheap)], results[REFERENCE]
    good, whole = MIN_ACCURACY
    remove = []
    if c["verdict_ok"] * whole < c["pairs"] * good:
        remove.append(f"точность связки {c['verdict_accuracy']:.1%} ниже {good / whole:.0%}")
    if len(c["false_support"]) > MAX_FALSE_SUPPORT:
        remove.append(f"ложных «подтверждает» {len(c['false_support'])}, больше {MAX_FALSE_SUPPORT}")
    priced = c["cost_per_pair_usd"] is not None and ref["cost_per_pair_usd"] is not None
    if priced and c["cost_per_pair_usd"] >= ref["cost_per_pair_usd"]:
        remove.append(f"связка не дешевле {REFERENCE}")
    if remove:
        return {"keep": False, "why": "; ".join(remove)}
    if not priced:
        return {"keep": None, "why": "нет цены вызова: сравнить по цене нельзя"}
    return {
        "keep": True,
        "why": f"точность не ниже 90 %, ложных «подтверждает» до {MAX_FALSE_SUPPORT}, дешевле {REFERENCE}",
    }


def overall(run: Run, required: list[str]) -> dict[str, Any]:
    """Решение по всем выборкам: «убрать», если хоть одна против; «оставить» — только если все «за»."""
    if run.failure:
        return {"keep": None, "why": run.failure}
    decisions = [(SAMPLES.get(s, s), d["decision"]) for s, d in run.samples.items()]
    against = [f"{name}: {d['why']}" for name, d in decisions if d["keep"] is False]
    if against:
        return {"keep": False, "why": "; ".join(against)}
    missing = [SAMPLES[s] for s in required if s not in run.samples]
    if missing:
        return {"keep": None, "why": f"нет выборки: {', '.join(missing)}"}
    unknown = [f"{name}: {d['why']}" for name, d in decisions if d["keep"] is None]
    if unknown:
        return {"keep": None, "why": "; ".join(unknown)}
    names = ", ".join(name for name, _ in decisions)
    return {"keep": True, "why": f"выборки {names}: {decisions[0][1]['why']}"}


# --- отчёт -------------------------------------------------------------------------------------------------


def _model_lines(name: str, r: dict[str, Any]) -> list[str]:
    price = (
        "нет данных" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}, пара ${r['cost_per_pair_usd']:.6f}"
    )
    lines = [
        f"  {name}: пар {r['pairs']}; вердикт {r['verdict_accuracy']:.1%}; тип {r['type_accuracy']:.1%}; "
        f"«не уверен» от модели {r['unsure_model_share']:.1%}; не разобрано {r['unparsed_share']:.1%}; "
        f"стоимость {price}"
    ]
    if "retried" in r:
        lines.append(
            f"    повторено на {REFERENCE}: {len(r['retried'])} из {r['pairs']} ({r['retried_share']:.1%})"
            + (f": {', '.join(r['retried'])}" if r["retried"] else "")
        )
    lines += [
        f"    ложные «подтверждает»: {', '.join(r['false_support']) or 'нет'}; "
        f"«не уверен» от модели: {', '.join(r['unsure_model']) or 'нет'}; "
        f"не разобраны: {', '.join(r['unparsed']) or 'нет'}",
        "    матрица (верный -> ответ): "
        + "; ".join(
            f"{row}: " + ", ".join(f"{col} {n}" for col, n in cols.items() if n)
            for row, cols in r["matrix"].items()
        ),
    ]
    return lines


def report_lines(run: Run, cheap: str, notes: list[str]) -> list[str]:
    lines = []
    if run.dry_run:
        lines.append("Заготовленные ответы (--dry-run): числа проверяют разбор и правило, а не модели.")
    lines += notes
    keep = {True: "оставить", False: "убрать", None: NO_DECISION}
    chain = cascade_name(cheap)
    for sample, d in run.samples.items():
        lines.append(f"Выборка {SAMPLES.get(sample, sample)} ({d['file']}): пар {d['pairs']}")
        results = d["results"]
        if chain in results:
            lines += _model_lines(f"связка {chain} (так роль работает в продукте)", results[chain])
        for model in dict.fromkeys([cheap, REFERENCE]):
            if model in results:
                lines += _model_lines(f"{model}{' (справочно)' if model == cheap else ''}", results[model])
        if "agreement" in d:
            lines.append(f"  Согласие {cheap} с {REFERENCE} по вердикту: {d['agreement']:.1%}")
            pairs = "; ".join(
                f"{x['id']}: эталон {x['expected']}, {cheap} {x['cheap']}, {REFERENCE} {x['reference']}"
                for x in d["disputed"]
            )
            lines.append(f"  Эталон расходится с обеими моделями (разметить вручную): {pairs or 'нет'}")
        if "decision" in d:
            lines.append(
                f"  Правило ADR-0007 на выборке: {keep[d['decision']['keep']]} — {d['decision']['why']}"
            )
    if run.failure:
        lines.append(f"Сбой вызова: {run.failure}. Пары этой пачки не оценены, замер остановлен.")
    assert run.decision is not None
    lines.append(f"Правило ADR-0007 (связка {chain}): {keep[run.decision['keep']]} — {run.decision['why']}")
    lines.append(SERVICE_NOTE)
    return lines


# --- запуск ------------------------------------------------------------------------------------------------


def dry_answers(path: Path = DRY) -> tuple[int | None, dict[str, dict[str, list[Reply]]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    canned: dict[str, dict[str, list[Reply]]] = {}
    for sample, models in data["samples"].items():
        canned[sample] = {
            key: [Reply(a.get("text", ""), a.get("cost_usd"), a.get("error")) for a in items]
            for key, items in models.items()
        }
    return data.get("batch"), canned


def default_models() -> tuple[str, str, str]:
    """Дешёвая модель, средняя (повтор «не уверен») и effort роли — из `config/routing.yaml`."""
    from strateg.routing import effective, load_routing

    table = load_routing()
    base = effective(table, classify.ROLE, "обычный")
    up = base.upgrade.to if base.upgrade is not None else base.level
    return base.model, table.levels[up], base.effort  # type: ignore[index]


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замер классификатора на размеченных выборках")
    parser.add_argument(
        "--models", help="дешёвая и средняя модели через запятую (по умолчанию — routing.yaml)"
    )
    parser.add_argument("--pairs", default=str(PAIRS), help="основная выборка")
    parser.add_argument("--holdout", default=str(HOLDOUT), help="контрольная выборка")
    parser.add_argument("--batch", type=int, help="пар в одном вызове (по умолчанию — все, как в продукте)")
    parser.add_argument("--effort", help="effort вызова (по умолчанию — из config/routing.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="заготовленные ответы: без сети и без claude")
    parser.add_argument("--answers", default=str(DRY), help="заготовленные ответы для --dry-run")
    parser.add_argument("--save", help="папка: итоги и сырые ответы в JSON, после каждой пачки")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    cheap, middle, effort = default_models()
    if args.models:
        names = [m.strip() for m in args.models.split(",") if m.strip()]
        if len(names) != 2:
            print("--models: нужны две модели через запятую — дешёвая и средняя", file=sys.stderr)
            return 2
        cheap, middle = names
    if middle != REFERENCE:
        print(f"--models: средняя модель — она же мерка сравнения — {REFERENCE}", file=sys.stderr)
        return 2
    effort = args.effort or effort
    notes: list[str] = []
    samples: dict[str, Path] = {"main": Path(args.pairs)}
    holdout = Path(args.holdout)
    if holdout.is_file():
        samples["holdout"] = holdout
    required = list(SAMPLES)

    if args.dry_run:
        size, canned = dry_answers(Path(args.answers))
        batch = args.batch or size
        for sample in list(samples):
            if sample not in canned:
                notes.append(
                    f"{SAMPLES[sample].capitalize()} выборка: заготовленных ответов нет — пропущена."
                )
                samples.pop(sample)
        required = list(samples)

        def ask(sample: str, model: str, index: int, _task: str) -> Reply:
            key = RETRY if model.startswith(f"{RETRY}:") else model
            items = canned.get(sample, {}).get(key, [])
            return items[index] if index < len(items) else Reply("", None, "нет заготовленного ответа")
    else:
        batch = args.batch
        exe = shutil.which("claude")
        if exe is None:
            print(
                "claude не найден в PATH: замер вызывает `claude -p`. Запустите в профиле, где Claude Code "
                "установлен и выполнен вход; разбор без claude проверяет --dry-run.",
                file=sys.stderr,
            )
            return 2
        role = classify.role_text()
        if not holdout.is_file():
            notes.append(f"Контрольной выборки нет ({_shown(holdout)}): решения не будет.")

        def ask(sample: str, model: str, index: int, task: str) -> Reply:
            name = model.split(":", 1)[1] if model.startswith(f"{RETRY}:") else model
            return ask_claude(exe, name, task, role, effort)

    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H%M%S")
    run = Run(
        dry_run=args.dry_run, batch=batch, save=Path(args.save) / f"{stamp}.json" if args.save else None
    )
    try:
        for sample, path in samples.items():
            measure_sample(run, sample, path, cheap, ask)
    except CallFailed:
        pass  # сбой записан в `run.failure` и в файл; решения нет
    run.decision = overall(run, required)
    run.flush()
    if args.json:
        print(json.dumps(run.summary(), ensure_ascii=False))
    else:
        print("\n".join(report_lines(run, cheap, notes)))
    if run.save is not None:
        print(f"Итоги и сырые ответы: {run.save}", file=sys.stderr)
    return 1 if run.decision["keep"] is None else 0


if __name__ == "__main__":
    raise SystemExit(main())
