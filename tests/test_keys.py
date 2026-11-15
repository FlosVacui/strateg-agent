"""Строки, похожие на ключи, до модели (ADR-0011, п. 4): общие шаблоны с упаковкой, метка `[ключ]`.

Значения ключей собираются из частей: тест не должен сам стать находкой сканера секретов репозитория.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from helpers import PUBLISHER, load_yaml, write_claims
from strateg import keys, pack, safety


def build(name: str) -> str:
    """Поддельное значение ключа нужного вида; ни одно не настоящее."""
    mix = ("Ab1Cd2Ef3G" * 8)[:40]
    return {
        "sk-ant": "sk" + "-ant-" + "api03-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7",
        "sk": "sk" + "-" + "proj1234567890ABCDEFGH12",
        "ghp": "gh" + "p_" + mix[:36],
        "gho": "gh" + "o_" + mix[:36],
        "ghs": "gh" + "s_" + mix[:36],
        "ghu": "gh" + "u_" + mix[:36],
        "ghr": "gh" + "r_" + mix[:36],
        "github_pat": "github" + "_pat_" + "11ABCDEFG0abcdefghij1234567890",
        "glpat": "gl" + "pat-" + "abcdefghij0123456789",
        "xoxb": "xo" + "xb-" + "1234567890-abcdefghij",
        "xoxp": "xo" + "xp-" + "1234567890-abcdefghij",
        "xoxa": "xo" + "xa-" + "2-1234567890-abcdefghij",
        "xoxs": "xo" + "xs-" + "1234567890-abcdefghij",
        "xoxr": "xo" + "xr-" + "1234567890-abcdefghij",
        "google": "AI" + "za" + mix[:35],
        "telegram": "123456789" + ":" + mix[:35],
        "jwt": "ey"
        + "JhbGciOiJIUzI1NiJ9"
        + "."
        + "ey"
        + "JzdWIiOiIxMjM0NTY3ODkwIn0"
        + "."
        + "abcDEF123_-xyzABC456",
        "aws": "AK" + "IA" + "IOSFODNN7EXAMPLE",
        "private": "-----BEGIN " + "RSA PRIVATE KEY" + "-----",
        "bearer": "Bearer " + "abcdef0123456789abcdef0123",
        "basic": "Basic " + "dXNlcjpwYXNzd29yZDEyMw==",
        "conn-pass": "S3cr3t" + "Passw0rd",
    }[name]


# (имя в build, текст со значением, ключевое слово в названии вида)
POSITIVE = [
    ("sk-ant", "ключ: {v}", "Anthropic"),
    ("sk", "OPENAI={v}", "sk-"),
    ("ghp", "git push https://{v}@example.com/repo.git", "GitHub"),
    ("gho", "token {v} выдан приложению", "GitHub"),
    ("ghs", "{v}", "GitHub"),
    ("ghu", "{v}", "GitHub"),
    ("ghr", "{v}", "GitHub"),
    ("github_pat", "export GH={v}", "github_pat_"),
    ("glpat", "CI_TOKEN в логе: {v}", "GitLab"),
    ("xoxb", "slack {v}", "Slack"),
    ("xoxp", "slack {v}", "Slack"),
    ("xoxa", "slack {v}", "Slack"),
    ("xoxs", "slack {v}", "Slack"),
    ("xoxr", "slack {v}", "Slack"),
    ("google", "key={v}&q=1", "Google"),
    ("telegram", "бот {v} отвечает", "Telegram"),
    ("jwt", "Cookie session={v}; path=/", "JWT"),
    ("aws", "aws_access_key_id = {v}", "AWS"),
    ("private", "{v}", "PRIVATE KEY"),
    ("bearer", "Authorization: {v}", "Bearer"),
    ("basic", "Authorization: {v}", "Basic"),
]

NEGATIVE = [
    "ghp_",
    "ghp_xxxxxxxx",
    "ghp_" + "короткий",
    "sk-ant-",
    "sk-ant-короткий",
    "sk-подписка-на-год-и-месяц",
    "glpat-",
    "glpat-короткий",
    "xoxb-",
    "xoxb-123",
    "AIza",
    "AIza_короткий",
    "123456789:коротко",
    "12:30:45",
    "время 10:15 и 123456789:",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0",  # две части: не JWT
    "Bearer",
    "Bearer токен",
    "Authorization: Bearer ${TOKEN}",
    "Authorization: Bearer <ваш токен>",
    "Authorization: Basic ${CREDENTIALS}",
    "Authorization: Basic короткий",
    "token=${GITHUB_TOKEN}",
    "token=<ключ>",
    "password=xxxxxxxxxxxx",
    "password=%PASSWORD%",
    "postgres://user:${DB_PASSWORD}@db.example.com:5432/app",
    "postgres://user:<password>@db.example.com/app",
    "postgres://user:xxxxxxxx@db.example.com/app",
    "postgres://user:password@db.example.com/app",
    "postgres://admin:[ключ]@db.example.com/app",  # метка сама ключом не считается
    "password=[ключ]",
    "postgres://db.example.com:5432/app",
    "https://example.com:8080/path",
    "https://user@example.com/page",
    "ftp://host:21/dir@x",
    "mailto:ivan@example.com",
    "Описание: токен — это маленькая строка, а ключ — большая.",
]


@pytest.mark.parametrize(("name", "template", "word"), POSITIVE)
def test_every_kind_of_key_is_found_and_replaced(name, template, word):
    value = build(name)
    text = "до " + template.format(v=value) + " после"
    masked, hits = keys.mask_keys(text)
    assert len(hits) == 1, (name, hits)
    assert word in hits[0].kind
    assert value not in masked and keys.KEY_LABEL in masked
    assert masked.startswith("до ") and masked.endswith(" после")


@pytest.mark.parametrize("text", NEGATIVE)
def test_look_alikes_and_placeholders_are_not_keys(text):
    assert keys.find_keys(text) == []
    assert keys.scan_lines(text) == []


def test_connection_string_hides_only_the_password():
    value = build("conn-pass")
    masked, hits = keys.mask_keys(f"DATABASE_URL=postgres://admin:{value}@db.example.com:5432/app")
    assert masked == "DATABASE_URL=postgres://admin:[ключ]@db.example.com:5432/app"
    assert "подключения" in hits[0].kind


def test_assignment_keeps_the_name_and_hides_the_value():
    masked, hits = keys.mask_keys("DB_PASSWORD=" + "Sup3rS3cretValue123" + "\nlog: ok")
    assert masked == "DB_PASSWORD=[ключ]\nlog: ok" and len(hits) == 1


def test_pack_and_safety_use_one_list_of_patterns():
    assert pack.keys is keys
    assert safety.keys is keys
    assert (
        pack.scan_text("a\n" + build("ghp"))
        == keys.scan_lines("a\n" + build("ghp"))
        == [(2, hits_name("ghp"))]
    )


def hits_name(name: str) -> str:
    return keys.find_keys(build(name))[0].kind


def test_inspect_text_replaces_keys_before_contacts():
    text = f"Звоните +7 (905) 123-45-67. Токен {build('ghp')} и почта help@example.com."
    rep = safety.inspect_text(text)
    assert build("ghp") not in rep.text
    assert rep.text.strip() == "Звоните [телефон]. Токен [ключ] и почта [почта]."
    assert rep.key_kinds == {hits_name("ghp"): 1}
    assert safety.KEYS_CATEGORY in rep.anonymized


def test_digits_inside_a_key_are_not_taken_for_a_phone_or_a_card():
    value = "sk" + "-" + "proj" + "89161234567" + "4111111111111111" + "ABCD"
    rep = safety.inspect_text(f"Ключ {value} стоит в конфиге")
    assert rep.text.strip() == "Ключ [ключ] стоит в конфиге" and rep.anonymized == [safety.KEYS_CATEGORY]
    masked, replacements, categories = safety.mask_personal(f"Ключ {value} стоит в конфиге")
    assert masked == "Ключ [ключ] стоит в конфиге" and categories == [safety.KEYS_CATEGORY]
    assert [(r.label, r.line) for r in replacements] == [("[ключ]", 1)]


def test_message_names_the_kind_and_count_but_never_the_value():
    value = build("ghp")
    rep = safety.inspect_text(f"{value}\nещё раз {build('gho')}")
    text = safety.keys_message(rep.key_kinds)
    assert "замен: 2" in text and "GitHub" in text
    assert value not in text and build("gho") not in text


def test_found_what_names_keys_and_personal_data():
    assert safety.found_what(["телефоны"]) == "персональные данные"
    assert safety.found_what(["ключи"]) == "строки, похожие на ключи"
    assert safety.found_what(["ключи", "телефоны"]) == "персональные данные и строки, похожие на ключи"


# --- время: недоверенный текст не должен подвесить проверку ---------------------------------------


@pytest.mark.parametrize(
    "chunk",
    [
        "a",
        "-",
        ".",
        "_",
        "sk-",
        "ghp_",
        "eyJ",
        "Bearer ",
        "http://a:",
        "token=",
        "1",
        "A",
        "xoxb-",
        "AIza",
        "://",
        "@",
        "\n",
        " ",
        "\t",
    ],
)
def test_key_search_is_fast_on_hostile_text(chunk):
    text = chunk * (100_000 // len(chunk))
    started = time.perf_counter()
    keys.find_keys(text)
    assert time.perf_counter() - started < 2.0, repr(chunk)


# --- add: ключ в материале ------------------------------------------------------------------------


@pytest.fixture
def demo(cli, home, private) -> Path:
    assert cli("new", "demo")[0] == 0
    return home / "demo"


def put(demo: Path, name: str, data: str) -> None:
    path = demo / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


NOTE = (
    "Заметка из чата поддержки.\nВ письме был токен {v} и просьба проверить подписку.\n"
    "Подписка стоит 199 рублей в месяц.\n"
)


def everything(demo: Path, *, skip_tmp: bool) -> str:
    parts = []
    for p in sorted(demo.rglob("*")):
        rel = p.relative_to(demo).parts
        if p.is_file() and not (skip_tmp and rel[0] == ".tmp"):
            parts.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def test_a_key_in_a_material_goes_through_the_same_confirmation_as_personal_data(demo, cli, private):
    value = build("ghp")
    put(demo, "чат.txt", NOTE.format(v=value))
    code, out, err = cli("add", "чат.txt")
    assert code == 0, err
    assert value not in out + err
    assert "[ключ]" in out and "строка 2" in out
    assert "строки, похожие на ключи" in err + out and "замен: 1" in out
    assert "add --confirm И-1" in out
    (s,) = load_yaml(demo / "sources.yaml")["sources"]
    assert s["anonymized"] == ["ключи"]
    text = (demo / s["text_path"]).read_text(encoding="utf-8")
    assert "[ключ]" in text and value not in text and "199 рублей" in text
    # исходник вне работы до подтверждения: в inputs/ его нет, ключа нет нигде, кроме .tmp/
    assert not (demo / "inputs" / "чат.txt").exists()
    assert (demo / ".tmp" / "pending" / "И-1" / "чат.txt").read_text(encoding="utf-8") == NOTE.format(v=value)
    assert value not in everything(demo, skip_tmp=True)
    assert cli("pack")[0] == 1  # до подтверждения упаковка отказывает
    # цитировать нельзя, пока человек не подтвердил замены
    claim = {
        "text": "Подписка стоит 199 рублей в месяц",
        "type": "факт",
        "subject": "цена подписки",
        "quotes": [{"source": "И-1", "text": "Подписка стоит 199 рублей в месяц", **PUBLISHER}],
    }
    code, _, err = cli("claim", "add", "--file", write_claims(demo / ".tmp", claim))
    assert code == 1 and "строки, похожие на ключи" in err and "add --confirm И-1" in err
    code, out, err = cli("add", "--confirm", "И-1")
    assert code == 0, err
    held = private / "demo" / "И-1" / "чат.txt"
    assert value in held.read_text(encoding="utf-8")  # исходник вне проекта и вне архива
    assert value not in everything(demo, skip_tmp=False)
    code, _, err = cli("claim", "add", "--file", write_claims(demo / ".tmp", claim, "c2.yaml"))
    assert code == 0, err


def test_a_key_in_json_output_is_named_by_kind_only(demo, cli):
    value = build("github_pat")
    put(demo, "чат.txt", NOTE.format(v=value))
    code, out, err = cli("add", "чат.txt", "--json")
    assert code == 0, err
    assert value not in out
    import json

    data = json.loads(out)
    assert data["anonymized"] == ["ключи"] and list(data["keys"].values()) == [1]
    assert data["needs_confirmation"] is True


def test_a_key_and_a_phone_in_one_material_are_both_replaced(demo, cli):
    value = build("xoxb")
    put(demo, "чат.txt", f"Звонок +7 (905) 123-45-67. Токен {value}.\nКонец.\n")
    code, out, err = cli("add", "чат.txt")
    assert code == 0, err
    (s,) = load_yaml(demo / "sources.yaml")["sources"]
    assert s["anonymized"] == ["телефоны", "ключи"]
    assert "персональные данные и строки, похожие на ключи" in out
    assert value not in out + (demo / s["text_path"]).read_text(encoding="utf-8")


# --- fetch: ключ в странице -----------------------------------------------------------------------


def test_a_key_on_a_page_is_replaced_before_the_snapshot_is_written(cli, net, home):
    from helpers import URL

    value = build("glpat")
    secret = build("conn-pass")
    page = (
        "<html><head><title>Заметки админа</title></head><body><main><h1>Заметки админа</h1>"
        "<p>Стоимость подписки в Москве — 199 рублей в месяц. Подписка работает на всех тарифах.</p>"
        f"<p>В примере конфигурации остался токен {value} и строка postgres://admin:{secret}@db.example.com/app.</p>"
        "<p>Подстановка token=${GITHUB_TOKEN} секретом не считается и остаётся в тексте как есть.</p>"
        "</main></body></html>"
    )
    net.add(URL, page)
    assert cli("new", "demo")[0] == 0
    code, out, err = cli("fetch", URL, "--json")
    assert code == 0, err
    assert value not in out and secret not in out
    import json

    data = json.loads(out)
    warning = next(w for w in data["warnings"] if "похожие на ключи" in w)
    assert "замен: 2" in warning and "GitLab" in warning and "строка подключения" in warning
    assert value not in warning and secret not in warning
    text = (home / "demo" / data["snapshot"]).read_text(encoding="utf-8")
    assert value not in text and secret not in text
    assert text.count("[ключ]") == 2 and "postgres://admin:[ключ]@db.example.com/app" in text
    assert "token=${GITHUB_TOKEN}" in text
    (source,) = load_yaml(home / "demo" / "sources.yaml")["sources"]
    assert "ключи" in source["anonymized"]
    # запись проекта ключа не содержит нигде, и упаковка проходит
    assert value not in everything(home / "demo", skip_tmp=False)
    code, out, err = cli("pack")
    assert code == 0, err
