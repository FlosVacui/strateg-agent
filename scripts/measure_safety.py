"""Замер проверок до модели на корпусе фикстур (ADR-0011, «Результат замера»).

Считает по видам: сколько известных значений найдено и заменено меткой, сколько ложных срабатываний на
отрицательных примерах; отдельно — время проверки текста в 200 КБ. Корпус — `tests/safety_measure.py`
(вымышленные данные, номера с верными контрольными суммами порождаются кодом с фиксированным зерном).

Запуск из корня репозитория:  uv run python scripts/measure_safety.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from safety_measure import measure  # noqa: E402
from strateg import safety  # noqa: E402


def timing() -> float:
    """Секунды на проверку текста в 200 КБ (`inspect_text`: ключи, контакты, закрытое, обращения к ИИ)."""
    base = (ROOT / "README.md").read_text(encoding="utf-8") + (
        ROOT / "docs" / "adr" / "0011-data-safety.md"
    ).read_text(encoding="utf-8")
    text = (base * (200_000 // len(base) + 1))[:200_000]
    safety.inspect_text(text)  # прогрев: первый вызов компилирует шаблоны
    started = time.perf_counter()
    for _ in range(3):
        safety.inspect_text(text)
    return (time.perf_counter() - started) / 3


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("| Вид | Найдено из известных | Ложных на отрицательных |")
    print("|---|---|---|")
    for kind in measure().values():
        found = f"{kind.found} из {kind.known}" if kind.known else "—"
        false = f"{kind.false} из {kind.negatives}" if kind.negatives else "—"
        print(f"| {kind.name} | {found} | {false} |")
    print(f"\nПроверка текста в 200 КБ: {timing() * 1000:.0f} мс")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
