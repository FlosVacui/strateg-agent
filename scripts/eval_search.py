"""Замер ADR-0012: поиск с триграммами, `unicode61` и простой поиск подстроки по файлам.

Запуск из корня репозитория:  uv run python scripts/eval_search.py [--json]

Проект и 20 запросов с известными ответами — `tests/search_corpus.py` (вымышленные тексты, 25 источников,
60 утверждений). Показатель — доля запросов, у которых верный ответ в первой пятёрке выдачи. Способы:
1. триграммы с приведением запроса: формы слова (`forms`), основа по правилу (`stem`), оба (`both`), без
   приведения (`exact`, только нижний регистр и ё -> е);
2. `unicode61`: слова без приведения и с формами слова (запасной путь без trigram);
3. простой поиск подстроки по файлам (как `grep -i` по словам запроса).

Контекст: размер выдачи `search` против размера файлов, которые пришлось бы прочесть для того же ответа.
Время: первый `search` с построением индекса и повторный, в процессе и целым процессом.
Папка проектов — временная, проект пользователя не затрагивается.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

TOP = 5
CATEGORIES = {
    "как в тексте": "как в тексте",
    "другой падеж": "падеж или число",
    "другое число": "падеж или число",
    "другая форма": "падеж или число",
    "беглая гласная": "беглая гласная",
    "синоним": "синоним",
}


def category(note: str) -> str:
    for key, name in CATEGORIES.items():
        if key in note:
            return name
    return "падеж или число"


def _naive_units(root: Path) -> list[tuple[str, str]]:
    """Что видит агент без поиска: файлы текстов и каждое утверждение из `claims.yaml` как куски текста."""
    units: list[tuple[str, str]] = []
    claims_text = (root / "claims.yaml").read_text(encoding="utf-8")
    for block in re.split(r"\n- id: ", claims_text)[1:]:
        units.append((block.splitlines()[0].strip(), block))
    sources = (root / "sources.yaml").read_text(encoding="utf-8")
    for m in re.finditer(r"- id: (И-\d+).*?text_path: (\S+)", sources, re.S):
        units.append((m.group(1), (root / m.group(2)).read_text(encoding="utf-8")))
    return units


def naive_top(units: list[tuple[str, str]], queries: list[str], k: int = TOP) -> list[str]:
    """Простой поиск подстроки: каждое слово запроса как `grep -i`; выше те места, где совпало больше слов."""
    words = list(dict.fromkeys(w for q in queries for w in re.findall(r"\w+", q.lower()) if len(w) > 2))
    scored = []
    for ref, text in units:
        low = text.lower()
        distinct = sum(1 for w in words if w in low)
        total = sum(low.count(w) for w in words)
        if distinct:
            scored.append((-distinct, -total, ref))
    scored.sort()
    return [ref for _d, _t, ref in scored[:k]]


def evaluate(root: Path) -> dict[str, Any]:
    """Все способы по всем запросам проекта `root`. Ничего не пишет в проект."""
    import search_corpus
    from strateg import project as proj
    from strateg import search

    p = proj.Project.load(root)
    queries = search_corpus.queries_with_answers(p)
    files = search.read_project_files(root)
    docs, notes = search.project_docs(files)
    areas = frozenset(search.DEFAULT_AREAS)
    indexes = {
        mode: search.Index(search.build(docs, "замер", mode, notes), mode, True, notes)
        for mode in ("trigram", "unicode61")
    }
    units = _naive_units(root)
    ways = [
        ("триграммы, формы слова (pymorphy3)", "trigram", "forms"),
        ("триграммы, основа по правилу", "trigram", "stem"),
        ("триграммы, формы и основа", "trigram", "both"),
        ("триграммы, без приведения слов", "trigram", "exact"),
        ("unicode61, формы слова", "unicode61", "forms"),
        ("unicode61, без приведения слов", "unicode61", "exact"),
        ("подстрока по файлам", None, None),
    ]
    table = []
    for name, mode, method in ways:
        ok: dict[int, bool] = {}
        at: dict[int, int] = {1: 0, 3: 0}
        reciprocal, shown, together = 0.0, 0, 0
        correct_total, extra_total, shown_total = 0, 0, 0
        for i, q in enumerate(queries):
            all_q = [q["query"], *q["also"]]
            if mode is None:
                everything = naive_top(units, all_q, 50)
            else:
                result = search.run_search(
                    [indexes[mode]], all_q, areas, limit=50, only_active=False, method=method or "forms"
                )
                everything = [h.ref for h in result.hits]
                together += not result.partial
            top = everything[:TOP]
            right = sum(1 for ref in top if ref in q["expect"])
            correct_total += right
            extra_total += len(top) - right
            shown_total += len(top)
            shown += len(everything)
            reciprocal += next(
                (1 / n for n, ref in enumerate(everything, start=1) if ref in q["expect"]), 0.0
            )
            ok[i] = bool(set(top) & set(q["expect"]))
            for k in at:
                at[k] += bool(set(top[:k]) & set(q["expect"]))
        by: dict[str, list[int]] = {}
        for i, q in enumerate(queries):
            by.setdefault(category(q["note"]), []).append(int(ok[i]))
        table.append(
            {
                "way": name,
                "mode": mode,
                "method": method,
                "found": sum(ok.values()),
                "of": len(queries),
                "share": sum(ok.values()) / len(queries),
                "mrr": reciprocal / len(queries),
                "precision": correct_total / shown_total if shown_total else 0.0,
                "extras": extra_total / len(queries),
                "mean_total": shown / len(queries),
                "together": together if mode is not None else None,
                "top1": at[1],
                "top3": at[3],
                "by_category": {k: [sum(v), len(v)] for k, v in sorted(by.items())},
                "missed": [queries[i]["query"] for i in ok if not ok[i]],
            }
        )
    for index in indexes.values():
        index.close()
    return {"queries": len(queries), "ways": table, "context": context(root, queries, units)}


def context(root: Path, queries: list[dict[str, Any]], units: list[tuple[str, str]]) -> dict[str, Any]:
    """Размер выдачи `search` против размера файлов, которые читает агент без поиска, в знаках."""
    from strateg import search

    claims_size = len((root / "claims.yaml").read_text(encoding="utf-8"))
    texts = {ref: len(text) for ref, text in units if ref.startswith("И-")}
    everything = claims_size + sum(texts.values())
    index = search.open_project_index(root)
    out_sizes, targeted = [], []
    try:
        for q in queries:
            result = search.run_search(
                [index],
                [q["query"], *q["also"]],
                frozenset(search.DEFAULT_AREAS),
                limit=search.DEFAULT_LIMIT,
                only_active=False,
            )
            out_sizes.append(len("\n".join(search.render(result, [q["query"]], list(search.DEFAULT_AREAS)))))
            sources = [r for r in q["expect"] if r.startswith("И-")]
            targeted.append(claims_size + sum(texts.get(r, 0) for r in sources))
    finally:
        index.close()
    mean = sum(out_sizes) / len(out_sizes)
    return {
        "search_chars": round(mean),
        "all_files_chars": everything,
        "answer_files_chars": round(sum(targeted) / len(targeted)),
        "share_of_all": mean / everything,
        "share_of_answer_files": mean / (sum(targeted) / len(targeted)),
    }


def timing(root: Path) -> dict[str, float]:
    """Время `search` на проекте замера: целым процессом и в процессе, без запуска Python.

    `startup_s` — пустая команда (`--version`): запуск Python и загрузка модулей без поиска.
    """
    from strateg import search

    env = {**os.environ, "STRATEG_PROJECT": root.name, "PYTHONUTF8": "1"}
    index = root / ".tmp" / "search.sqlite"
    base = [sys.executable, "-m", "strateg.cli"]

    def process(*args: str) -> float:
        started = time.perf_counter()
        done = subprocess.run([*base, *args], env=env, capture_output=True, check=False)
        if done.returncode != 0:
            raise SystemExit(done.stderr.decode("utf-8", "replace"))
        return time.perf_counter() - started

    index.unlink(missing_ok=True)
    query = "нехватка курьеров в Тюмени"
    startup = process("--version")
    first = process("search", query, "--json")
    again = process("search", query, "--json")
    probe = (
        "import time; from strateg import safety; t = time.perf_counter(); "
        "safety._analyzer(); print(time.perf_counter() - t)"
    )
    morph = float(
        subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True).stdout
    )
    index.unlink(missing_ok=True)
    areas = frozenset(search.DEFAULT_AREAS)
    started = time.perf_counter()
    handle = search.open_project_index(root)
    built = time.perf_counter() - started
    search.run_search([handle], [query], areas, limit=8, only_active=False)
    handle.close()
    started = time.perf_counter()
    handle = search.open_project_index(root)
    search.run_search([handle], [query], areas, limit=8, only_active=False)
    handle.close()
    return {
        "startup_s": startup,
        "first_process_s": first,
        "second_process_s": again,
        "morphology_load_s": morph,
        "index_build_in_process_s": built,
        "second_in_process_s": time.perf_counter() - started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="strateg-eval-") as tmp:
        os.environ["STRATEG_PROJECTS_DIR"] = tmp
        import search_corpus
        from strateg import project as proj

        search_corpus.build_project("corpus")
        root = proj.projects_root() / "corpus"
        data = evaluate(root)
        data["timing"] = timing(root)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print(f"Запросов: {data['queries']}, верный ответ в первой пятёрке:")
    for way in data["ways"]:
        cats = "; ".join(f"{k} {a}/{b}" for k, (a, b) in way["by_category"].items())
        print(
            f"  {way['way']:<40} {way['found']:>2} из {way['of']} ({way['share']:.0%}), "
            f"в первой тройке {way['top3']}, на первом месте {way['top1']}, MRR {way['mrr']:.2f}, "
            f"точность в пятёрке {way['precision']:.0%}, лишних мест на запрос {way['extras']:.1f}, "
            f"мест в выдаче в среднем {way['mean_total']:.0f}, все слова вместе {way['together']}  [{cats}]"
        )
        if way["missed"]:
            print(f"      не найдено: {'; '.join(way['missed'])}")
    c = data["context"]
    print(
        f"Контекст: выдача {c['search_chars']} знаков; все файлы {c['all_files_chars']} знаков "
        f"({c['share_of_all']:.1%}), файлы с ответом {c['answer_files_chars']} "
        f"({c['share_of_answer_files']:.1%})."
    )
    t = data["timing"]
    print(
        f"Время: пустая команда {t['startup_s']:.2f} с; первый search {t['first_process_s']:.2f} с, "
        f"повторный {t['second_process_s']:.2f} с (целым процессом, с запуском Python)."
    )
    print(
        f"В процессе: загрузка pymorphy3 {t['morphology_load_s']:.2f} с, построение индекса "
        f"{t['index_build_in_process_s']:.2f} с, повторный поиск {t['second_in_process_s']:.3f} с."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
