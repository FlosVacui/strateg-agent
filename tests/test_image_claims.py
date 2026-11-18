"""Утверждения по изображениям и снимкам экрана: цитата с местом «экран …; элемент …» (ADR-0005, ADR-0011)."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from helpers import FIXTURE, claims_of, write_claims
from strateg.models import Quote, behaviour_words, parse_image_locator
from strateg.project_check import check_project
from test_project_check import assert_error, claim, edit_yaml, errors_of

LOCATOR = "экран Оплата; элемент серая кнопка «Подтвердить» внизу справа"
QUOTE = "серая кнопка «Подтвердить» внизу справа"
OBSERVATION = "На экране оплаты кнопка «Подтвердить» серая"


def png_bytes(seed: int = 0) -> bytes:
    """Свой маленький PNG 4x4 без людей и чужих интерфейсов: цвет зависит от seed."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    row = b"\x00" + bytes([seed % 256, 120, 200]) * 4
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * 4))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def add_image(demo: Path, cli, name: str = "оплата.png", seed: int = 0, confirm: bool = True) -> None:
    path = demo / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(seed))
    assert cli("add", name)[0] == 0
    if confirm:
        assert cli("add", "--confirm", "И-1")[0] == 0


def fact(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "text": OBSERVATION,
        "type": "факт",
        "subject": "экран оплаты, кнопка «Подтвердить»",
        "quotes": [
            {
                "source": "И-1",
                "text": QUOTE,
                "locator": LOCATOR,
                "publisher": "автор снимка",
                "publisher_kind": "материал пользователя",
                "self_reported": False,
            }
        ],
    }
    data.update(extra)
    return data


def add_claim(demo: Path, cli, data: dict[str, Any], name: str = "c.yaml") -> tuple[int, str, str]:
    return cli("claim", "add", "--file", write_claims(demo / ".tmp", data, name))


def test_fact_about_the_interface_is_recorded_with_screen_and_element(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact())
    assert code == 0, err
    (c,) = claims_of(demo)
    (q,) = c["quotes"]
    assert q["locator"] == LOCATOR
    assert q["check"]["method"] == "по изображению" and q["check"]["similarity"] == 100
    assert q["check"]["snapshot_sha256"] == hashlib.sha256(png_bytes()).hexdigest()
    assert check_project(demo) == []


def test_quote_from_an_unconfirmed_image_is_refused_and_nothing_is_written(demo, cli):
    add_image(demo, cli, confirm=False)
    code, _, err = add_claim(demo, cli, fact())
    assert code == 1 and "не подтвердил" in err and "add --confirm И-1" in err
    assert claims_of(demo) == []
    assert any("на изображении нет персональных данных" in e for e in check_project(demo))
    assert cli("add", "--confirm", "И-1")[0] == 0
    assert add_claim(demo, cli, fact(), "c2.yaml")[0] == 0


@pytest.mark.parametrize(
    "locator",
    [
        None,
        "экран Оплата",
        "элемент кнопка",
        "экран ; элемент кнопка",
        "экран Оплата; элемент ",
        "экран Оплата; элемент —",
        "элемент кнопка; экран Оплата",
        "стр. 3",
        "экран Оплата, элемент кнопка",
    ],
)
def test_locator_is_required_and_parsed_strictly(demo, cli, locator):
    add_image(demo, cli)
    data = fact()
    if locator is None:
        del data["quotes"][0]["locator"]
    else:
        data["quotes"][0]["locator"] = locator
    code, _, err = add_claim(demo, cli, data)
    assert code == 1 and "экран <название>; элемент <что и где>" in err
    assert claims_of(demo) == []


def test_empty_locator_is_refused_by_the_schema(demo, cli):
    add_image(demo, cli)
    data = fact()
    data["quotes"][0]["locator"] = ""
    assert add_claim(demo, cli, data)[0] == 1
    assert claims_of(demo) == []


def test_locator_parser():
    assert parse_image_locator("экран Оплата; элемент серая кнопка") == ("Оплата", "серая кнопка")
    assert parse_image_locator("  Экран  Главная ;  Элемент  меню слева ") == ("Главная", "меню слева")
    assert parse_image_locator("экран А; элемент Б; элемент В") is None
    assert parse_image_locator(None) is None


def test_fact_about_user_behaviour_is_refused_but_a_hypothesis_is_accepted(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact(text="Пользователи не находят кнопку «Подтвердить»"))
    assert code == 1 and "поведении или желаниях людей" in err and "гипотезой" in err
    assert claims_of(demo) == []
    assert add_claim(demo, cli, fact(), "c2.yaml")[0] == 0
    hyp = fact(
        text="Пользователи не находят кнопку «Подтвердить» из-за серого цвета",
        type="гипотеза",
        subject="заметность кнопки «Подтвердить»",
        check_method="пять интервью с показом экрана: найти кнопку за 10 секунд",
    )
    code, _, err = add_claim(demo, cli, hyp, "c3.yaml")
    assert code == 0, err
    assert [c["type"] for c in claims_of(demo)] == ["факт", "гипотеза"]
    assert check_project(demo) == []


def test_behaviour_words():
    assert behaviour_words("Клиенты хотят платить сразу") == "хотят"
    assert behaviour_words("Большинство пропускает баннер")
    assert behaviour_words("На экране оплаты кнопка серая") is None


def test_image_changed_after_registration_is_refused(demo, cli):
    add_image(demo, cli)
    (demo / "inputs" / "оплата.png").write_bytes(png_bytes(7))
    code, _, err = add_claim(demo, cli, fact())
    assert code == 1 and "изменён после регистрации" in err
    assert claims_of(demo) == []


def test_image_quote_under_a_synthetic_claim_is_refused(demo, cli):
    add_image(demo, cli)
    code, _, err = add_claim(demo, cli, fact(type="синтетика"))
    assert code == 1 and "синтетика" in err


def test_repeat_of_the_same_image_claim_is_not_a_second_record(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    code, _, _ = add_claim(demo, cli, fact(), "c2.yaml")
    assert code == 0 and len(claims_of(demo)) == 1


# --- check ловит ручные правки реестров ---------------------------------------------------------------


def test_check_flags_unconfirmed_image_under_an_existing_quote(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "sources.yaml", lambda d: d["sources"][0].update(no_personal_data_confirmed=False))
    errors = check_project(demo)
    assert any("claims.yaml" in e and "не подтвердил" in e and "нет персональных данных" in e for e in errors)


def test_check_flags_a_missing_or_broken_locator(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0]["quotes"][0].pop("locator"))
    assert any("claims.yaml" in e and "элемент <что и где>" in e for e in check_project(demo))
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0]["quotes"][0].update(locator="экран Оплата"))
    assert any("claims.yaml" in e and "элемент <что и где>" in e for e in check_project(demo))


def test_check_flags_a_changed_image_and_a_behaviour_fact(demo, cli):
    add_image(demo, cli)
    assert add_claim(demo, cli, fact())[0] == 0
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text="Пользователи не находят кнопку"))
    assert any("поведении или желаниях людей" in e for e in check_project(demo))
    edit_yaml(demo, "claims.yaml", lambda d: d["claims"][0].update(text=OBSERVATION))
    assert check_project(demo) == []
    (demo / "inputs" / "оплата.png").write_bytes(png_bytes(9))
    assert any("изменён после регистрации" in e for e in check_project(demo))


def test_model_requires_a_strict_locator_for_the_image_method():
    q: dict[str, Any] = {
        "source": "И-1",
        "text": QUOTE,
        "locator": LOCATOR,
        "check": {
            "method": "по изображению",
            "similarity": 100,
            "snapshot_sha256": "0" * 64,
            "checked_at": "2026-11-06T10:00:00+03:00",
        },
    }
    Quote.model_validate(q)
    q["locator"] = "стр. 2"
    with pytest.raises(ValueError, match="место записывается строго"):
        Quote.model_validate(q)


# --- пример проекта с изображением ----------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    shutil.copytree(FIXTURE, dest)
    body = png_bytes(3)
    (dest / "inputs").mkdir(exist_ok=True)
    (dest / "inputs" / "экран.png").write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()

    def add_source(d: dict[str, Any]) -> None:
        d["sources"].append(
            {
                "id": "И-9",
                "kind": "изображение",
                "path": "inputs/экран.png",
                "access": "получен",
                "untrusted": True,
                "material_sha256": sha,
                "no_personal_data_confirmed": True,
            }
        )

    def add_claims(d: dict[str, Any]) -> None:
        base = json.loads(json.dumps(claim(d, "У-2")))
        base.update(
            id="У-9",
            text=OBSERVATION,
            subject="экран оплаты, кнопка «Подтвердить»",
            replaces=None,
            plan_question=None,
        )
        base["quotes"] = [
            {
                "source": "И-9",
                "text": QUOTE,
                "locator": LOCATOR,
                "publisher": "автор снимка",
                "publisher_kind": "материал пользователя",
                "self_reported": False,
                "check": {
                    "method": "по изображению",
                    "similarity": 100,
                    "snapshot_sha256": sha,
                    "checked_at": "2026-11-06T10:20:00+03:00",
                },
            }
        ]
        base.pop("verdict", None)
        d["claims"].append(base)
        d["claims"].append(
            {
                "id": "У-10",
                "text": "Пользователи не находят серую кнопку «Подтвердить»",
                "type": "гипотеза",
                "origin": "агент",
                "step": "2б",
                "attempt": 1,
                "subject": "заметность кнопки «Подтвердить»",
                "basis": ["У-9"],
                "check_method": "пять интервью с показом экрана",
                "status": "действует",
                "status_history": [{"status": "действует", "at": "2026-11-06T10:21:00+03:00", "by": "агент"}],
            }
        )

    edit_yaml(dest, "sources.yaml", add_source)
    edit_yaml(dest, "claims.yaml", add_claims)
    return dest


def test_example_with_an_image_fact_is_clean(project):
    assert errors_of(project) == []


def test_image_method_on_a_text_source_is_an_error(project):
    edit_yaml(
        project,
        "claims.yaml",
        lambda d: claim(d, "У-2")["quotes"][0].update(
            locator=LOCATOR, check={**claim(d, "У-2")["quotes"][0]["check"], "method": "по изображению"}
        ),
    )
    assert_error(project, "claims.yaml", "бывает только у цитаты с изображения")


def test_recommendation_on_image_facts_alone_needs_a_hypothesis(project):
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-9"]))
    assert_error(project, "options.yaml, recommendation", "только на фактах с изображений", "У-9")
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-9", "У-10"]))
    assert not any("только на фактах с изображений" in e for e in errors_of(project))
    edit_yaml(project, "options.yaml", lambda d: d["recommendation"].update(claims=["У-9", "У-2"]))
    assert not any("только на фактах с изображений" in e for e in errors_of(project))


def test_yaml_dump_keeps_the_fixture_readable():
    assert yaml.safe_load("a: 1") == {"a": 1}


# --- документы и отказ для аудио и видео ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def test_docs_describe_images_and_the_limits():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Аудио и видео" in readme and "экран <название>; элемент <что и где>" in readme
    assert "Playwright не подключён" in readme
    adr5 = (ROOT / "docs" / "adr" / "0005-claims-quote-check.md").read_text(encoding="utf-8")
    assert "по изображению" in adr5 and "не признак вида источника" in adr5
    adr11 = (ROOT / "docs" / "adr" / "0011-data-safety.md").read_text(encoding="utf-8")
    assert "**Изображения.**" in adr11 and "add --confirm" in adr11
    adr13 = (ROOT / "docs" / "adr" / "0013-mcp.md").read_text(encoding="utf-8")
    assert "разбор по снимкам экрана от человека сделан" in adr13
    schema = json.loads((ROOT / "schemas" / "claims.schema.json").read_text(encoding="utf-8"))
    assert "по изображению" in json.dumps(schema, ensure_ascii=False)
