"""Замер классификатора (ADR-0007, ADR-0008): точность на размеченной выборке против Sonnet и по цене.

Выборка — `evals/classifier/pairs.yaml`. Задание каждой пачке пар собирает тот же код, что и
`strateg claim pairs` (`strateg.classify.render_task` с текстом роли), ответ разбирает тот же строгий
разбор, что и `strateg claim verdict --file`: неразобранная пара — «не уверен».

Замер (нужен вход в claude; каждый вызов — `claude -p` с выключенными инструментами, вне репозитория):
    uv run python scripts/eval_classifier.py --models haiku,sonnet --save evals/classifier/results
Проверка разбора без сети и без claude, на заготовленных ответах `evals/classifier/dry-run.yaml`:
    uv run python scripts/eval_classifier.py --dry-run

Печатает по каждой модели: точность по вердикту и по типу, матрицу ошибок, долю «не уверен», ложные
«подтверждает», стоимость по `total_cost_usd` и цену одной пары; затем согласие дешёвой модели с Sonnet и
вывод по правилу ADR-0007: точность ниже 90 % или не дешевле Sonnet — роль убираем.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from strateg import classify

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "evals" / "classifier" / "pairs.yaml"
DRY = ROOT / "evals" / "classifier" / "dry-run.yaml"
VERDICT_ROWS = ("подтверждает", "частично", "не подтверждает")
GROUPS = ("подтверждает", "не подтверждает", "частично", "неверный тип")
MIN_ACCURACY = 0.90  # ADR-0007
REFERENCE = "sonnet"
DEFAULT_BATCH = 10
TIMEOUT_S = 600

Answer = tuple[str, float | None]  # текст ответа и total_cost_usd вызова
Ask = Callable[[str, str, int], Answer]  # модель, задание, номер пачки -> ответ


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
    quotes = []
    for q in p["quotes"]:
        text, truncated = classify.cut(q["text"])
        quotes.append(classify.PairQuote(q["source"], text, truncated, False))
    return classify.Pair(
        p["id"], p["type"], classify.one_line(p["text"]), p.get("period"), p.get("region"), tuple(quotes)
    )


def batches(pairs: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [pairs[i : i + size] for i in range(0, len(pairs), size)]


def ask_claude(model: str, task: str, _index: int, effort: str | None = None) -> Answer:
    """Один вызов `claude -p`: без инструментов, без сохранения сессии, в пустой папке вне репозитория."""
    argv = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", ""]
    argv += ["--no-session-persistence"]
    if effort:
        argv += ["--effort", effort]
    with tempfile.TemporaryDirectory() as cwd:
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
    if r.returncode != 0:
        return f"(ошибка вызова: код {r.returncode}) {r.stderr.strip()[:300]}", None
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout, None  # ответ не JSON: разбор найдёт строки, если они есть
    cost = out.get("total_cost_usd")
    return str(out.get("result") or ""), float(cost) if isinstance(cost, int | float) else None


def dry_answers(path: Path = DRY) -> tuple[int, dict[str, list[Answer]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = {m: [(a["text"], a.get("cost_usd")) for a in items] for m, items in data["models"].items()}
    return int(data["batch"]), models


def run_model(model: str, pairs: list[dict[str, Any]], size: int, ask: Ask) -> dict[str, Any]:
    role = classify.role_text()
    parsed: dict[str, classify.Parsed] = {}
    cost: float | None = 0.0
    raw = []
    unknown: list[str] = []
    for n, chunk in enumerate(batches(pairs, size)):
        ids = [p["id"] for p in chunk]
        text, price = ask(model, classify.render_task([to_pair(p) for p in chunk], role), n)
        answer = classify.parse_answer(text, ids)
        parsed.update(answer.items)
        unknown += answer.unknown
        cost = None if cost is None or price is None else cost + price
        raw.append({"ids": ids, "text": text, "cost_usd": price})
    return {"model": model, **score(pairs, parsed, cost), "unknown_ids": unknown, "answers": raw}


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
    unsure = sum(1 for x in parsed.values() if x.verdict == "не уверен")
    return {
        "pairs": n,
        "verdict_accuracy": round(verdict_ok / n, 4),
        "type_accuracy": round(type_ok / n, 4),
        "unsure_share": round(unsure / n, 4),
        "unparsed": sorted((k for k, v in parsed.items() if not v.parsed), key=_num),
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


def decision(results: dict[str, dict[str, Any]], cheap: str) -> dict[str, Any]:
    """Правило ADR-0007 для дешёвой модели против Sonnet."""
    c, ref = results.get(cheap), results.get(REFERENCE)
    if c is None or ref is None or cheap == REFERENCE:
        return {"keep": None, "why": "нужны обе модели: дешёвая и sonnet"}
    if c["verdict_accuracy"] < MIN_ACCURACY:
        return {"keep": False, "why": f"точность {c['verdict_accuracy']:.1%} ниже {MIN_ACCURACY:.0%}"}
    if c["cost_per_pair_usd"] is None or ref["cost_per_pair_usd"] is None:
        return {"keep": None, "why": "нет цены вызова: сравнить по цене нельзя"}
    if c["cost_per_pair_usd"] >= ref["cost_per_pair_usd"]:
        return {"keep": False, "why": "не дешевле sonnet"}
    return {"keep": True, "why": "точность не ниже 90 % и дешевле sonnet"}


def report_lines(results: dict[str, dict[str, Any]], verdict: dict[str, Any], cheap: str) -> list[str]:
    lines = []
    for model, r in results.items():
        price = (
            "нет данных"
            if r["cost_usd"] is None
            else f"${r['cost_usd']:.4f}, пара ${r['cost_per_pair_usd']:.6f}"
        )
        lines += [
            f"{model}: пар {r['pairs']}; вердикт {r['verdict_accuracy']:.1%}; тип {r['type_accuracy']:.1%}; "
            f"«не уверен» {r['unsure_share']:.1%}; стоимость {price}",
            f"  ложные «подтверждает»: {', '.join(r['false_support']) or 'нет'}; "
            f"не разобраны: {', '.join(r['unparsed']) or 'нет'}",
            "  матрица (верный -> ответ): "
            + "; ".join(
                f"{row}: " + ", ".join(f"{col} {n}" for col, n in cols.items() if n)
                for row, cols in r["matrix"].items()
            ),
        ]
    if cheap in results and REFERENCE in results and cheap != REFERENCE:
        lines.append(
            f"Согласие {cheap} с {REFERENCE} по вердикту: {agreement(results[cheap], results[REFERENCE]):.1%}"
        )
    keep = {True: "оставить", False: "убрать", None: "решения нет"}[verdict["keep"]]
    lines.append(f"Правило ADR-0007 ({cheap}): {keep} — {verdict['why']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замер классификатора на размеченной выборке")
    parser.add_argument("--models", default="haiku,sonnet", help="модели через запятую; первая — дешёвая")
    parser.add_argument("--pairs", default=str(PAIRS), help="файл выборки")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="пар в одном вызове")
    parser.add_argument("--effort", help="effort вызова (по умолчанию — из config/routing.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="заготовленные ответы: без сети и без claude")
    parser.add_argument("--answers", default=str(DRY), help="заготовленные ответы для --dry-run")
    parser.add_argument("--save", help="папка: записать итоги и сырые ответы в JSON")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    pairs = load_pairs(Path(args.pairs))
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    size = args.batch
    if args.dry_run:
        size, canned = dry_answers(Path(args.answers))
        models = [m for m in models if m in canned]

        def ask(model: str, _task: str, index: int) -> Answer:
            return canned[model][index]
    else:
        effort = args.effort
        if effort is None:
            from strateg.routing import load_routing

            effort = load_routing().roles[classify.ROLE].effort

        def ask(model: str, task: str, index: int) -> Answer:
            return ask_claude(model, task, index, effort)

    results = {m: run_model(m, pairs, size, ask) for m in models}
    cheap = models[0] if models else ""
    verdict = decision(results, cheap)
    summary = {
        "dry_run": args.dry_run,
        "batch": size,
        "results": {m: {k: v for k, v in r.items() if k != "answers"} for m, r in results.items()},
        "agreement": agreement(results[cheap], results[REFERENCE])
        if cheap in results and REFERENCE in results and cheap != REFERENCE
        else None,
        "decision": verdict,
    }
    if args.save:
        folder = Path(args.save)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H%M")
        full = {**summary, "results": results}
        (folder / f"{stamp}.json").write_text(
            json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        if args.dry_run:
            print("Заготовленные ответы (--dry-run): числа проверяют разбор, а не модели.")
        print("\n".join(report_lines(results, verdict, cheap)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
