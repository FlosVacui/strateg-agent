"""Общие данные тестов CLI: адрес страницы, цитата, готовое утверждение, запись файла утверждений."""

from __future__ import annotations

from pathlib import Path

import yaml

URL = "https://example.com/protection"
QUOTE = "Стоимость подписки в Москве — 199 рублей в месяц"
PUBLISHER = {
    "publisher": "компания «Пример»",
    "publisher_kind": "официальный первоисточник",
    "self_reported": True,
}


def fact(quote: str = QUOTE, **extra) -> dict:
    data = {
        "text": "Подписка «Защита» в Москве стоит 199 рублей в месяц",
        "type": "факт",
        "subject": "цена подписки «Защита»",
        "period": "2026",
        "region": "RU-MOW",
        "quotes": [{"source": "И-1", "text": quote, **PUBLISHER}],
    }
    data.update(extra)
    return data


def write_claims(tmp_path, data, name="claims.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def claims_of(root):
    return yaml.safe_load((root / "claims.yaml").read_text(encoding="utf-8"))["claims"]


def open_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "pages" / name).read_text(encoding="utf-8")
