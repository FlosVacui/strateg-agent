"""Общие данные тестов CLI: адрес страницы, цитата, готовое утверждение, запись файла утверждений."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

URL = "https://example.com/protection"
QUOTE = "Стоимость подписки в Москве — 199 рублей в месяц"
# Независимый источник: рекомендация и обещание держатся не только на заявлении компании о себе.
PUBLISHER = {
    "publisher": "компания «Пример-Аналитика»",
    "publisher_kind": "отраслевое исследование",
    "self_reported": False,
}
SELF_REPORTED = {
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


FRAME = {
    "decision": {"value": "Как продавать защиту семьям", "origin": "ответ человека"},
    "decision_space": ["продавать отдельной подпиской", "включить в тарифы"],
    "deadline": {"value": "сегодня", "origin": "допущение"},
    "research_question": {"value": "Как продавать защиту", "origin": "из запроса"},
    "recipient": {"value": "руководитель продукта", "origin": "допущение"},
    "constraints": {"value": "только открытые источники", "origin": "из запроса"},
    "data": {"value": "страница услуги", "origin": "из запроса"},
    "region": {"value": "Москва", "origin": "допущение"},
    "success_criteria": {"value": "рекомендация и план проверки", "origin": "допущение"},
    "out_of_scope": {"value": "доработка услуги", "origin": "допущение"},
}


def put(cli, **kinds):
    """`strateg write` со стандартного ввода: словарь «вид -> содержимое»."""
    code, out, err = cli("write", "--file", "-", stdin=json.dumps(kinds, ensure_ascii=False))
    assert code == 0, err
    return out


def put_frame(cli, **override):
    """Рамка для ответа на В1."""
    frame = {**FRAME, **override}
    return put(cli, frame=frame)


FIXTURE = Path(__file__).parent / "fixtures" / "project_valid"
KINDS = ("frame", "plan", "gaps", "options", "promises", "verification", "decisions")


def fixture_outputs(remap: dict[int, int] | None = None) -> dict[str, dict]:
    """Файлы выхода примера как вход `write`: без `schema_version` и `version`.

    `remap` переименовывает номера утверждений (У-2 -> У-1 и так далее) под другой набор утверждений.
    """
    out = {}
    for kind in KINDS:
        data = yaml.safe_load((FIXTURE / f"{kind}.yaml").read_text(encoding="utf-8"))
        data.pop("schema_version")
        data.pop("version", None)
        if remap:
            text = json.dumps(data, ensure_ascii=False)
            text = re.sub(r"У-(\d+)", lambda m: f"У-{remap.get(int(m.group(1)), int(m.group(1)))}", text)
            data = json.loads(text)
        out[kind] = data
    return out


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 всех файлов папки: по ним проверяют, что после отказа папка такая же, как до команды."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def journal_of(root: Path) -> list[dict]:
    text = (root / "journal.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
