"""`strateg search`: границы слов, числа, области до предела кусков, чужие поля, порча индекса, CRLF и BOM."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

import search_corpus
from helpers import write_claims
from strateg import project as proj
from strateg import search, storage
from strateg.search import AREA_CLAIMS, AREA_SOURCES, Chunk, Doc


@pytest.fixture
def corpus(home, clock, cli) -> Path:
    search_corpus.build_project("corpus")
    assert cli("use", "corpus")[0] == 0
    return home / "corpus"


def ask(cli, *argv: str) -> dict:
    code, out, err = cli("search", *argv, "--json")
    assert code == 0, out + err
    return json.loads(out)


def refs(data: dict) -> list[str]:
    return [r["id"] for r in data["results"]]


def claim_meta(**extra) -> dict:
    meta = {
        "type": "факт",
        "origin": "источник",
        "status": "действует",
        "replaced_by": None,
        "replaced_by_note": None,
        "replaces": None,
        "dispute": None,
        "suspicious": False,
        "reason": None,
        "sources": [],
    }
    return meta | extra


def source_doc(ref: str, text: str) -> Doc:
    meta = {"pages": search.page_marks(text), "kind": "страница", "untrusted": True}
    return Doc(AREA_SOURCES, ref, f"sources/{ref}.txt", meta, search.split_chunks(text))


def claim_doc(ref: str, text: str, **meta) -> Doc:
    return Doc(AREA_CLAIMS, ref, text, claim_meta(**meta), [Chunk(0, text)])


def memory(docs: list[Doc], mode: str = "trigram") -> search.Index:
    return search.Index(search.build(docs, "тест", mode, []), mode, True, [])


def find(index: search.Index, query: str, *, areas=None, only_active=False, limit=50) -> search.Result:
    return search.run_search(
        [index], [query], frozenset(areas or search.AREAS), limit=limit, only_active=only_active
    )


# --- область отбирается до предела числа кусков ----------


def big_sources(n_docs: int = 30, per_doc: int = 15) -> list[Doc]:
    sentence = "Доставка заказа идёт по городу, доставка бесплатная, доставка быстрая и доставка надёжная. "
    return [source_doc(f"И-{i}", (sentence * 7 + "\n\n") * per_doc) for i in range(1, n_docs + 1)]


def test_claims_are_found_when_snapshot_chunks_with_the_word_exceed_the_limit():
    docs = big_sources()
    assert sum(len(d.chunks) for d in docs) > search.CANDIDATES
    docs += [claim_doc(f"У-{n}", "Одна доставка в день") for n in (1, 2, 3)]
    index = memory(docs)
    only_claims = find(index, "доставка", areas={AREA_CLAIMS})
    assert {h.ref for h in only_claims.hits} == {"У-1", "У-2", "У-3"}
    default = find(index, "доставка", areas=search.DEFAULT_AREAS, limit=500)
    assert {"У-1", "У-2", "У-3"} <= {h.ref for h in default.hits}


def test_only_active_is_applied_before_the_limit_too():
    docs = [claim_doc(f"У-{n}", "Доставка " * 40, status="отозвано") for n in range(1, 450)]
    docs.append(claim_doc("У-900", "Доставка одна"))
    index = memory(docs)
    assert [h.ref for h in find(index, "доставка", only_active=True).hits] == ["У-900"]
    assert find(index, "доставка", areas={AREA_CLAIMS}).total > 1


# --- слово как слово ----------


def test_a_form_is_a_word_not_a_fragment():
    index = memory(
        [
            source_doc("И-1", "Оценка в магазине, процент вырос, ценность товара."),
            source_doc("И-2", "Цена доставки выросла."),
            source_doc("И-3", "Нет цен на товар."),
        ]
    )
    assert [h.ref for h in find(index, "цена").hits] == ["И-2", "И-3"] or {
        h.ref for h in find(index, "цена").hits
    } == {"И-2", "И-3"}
    hit = find(index, "цену").hits[0]
    assert hit.offset == 0 and hit.excerpt.startswith("Цена")


def test_word_boundary_holds_in_unicode61_and_plain_modes_too():
    docs = [source_doc("И-1", "Оценка и процент."), source_doc("И-2", "Цена.")]
    for mode in ("unicode61", "plain"):
        assert [h.ref for h in find(memory(docs, mode), "цена").hits] == ["И-2"], mode


def test_search_for_a_price_word_in_the_project_has_no_fragment_noise(corpus, cli):
    data = ask(cli, "цена", "--limit", "50")
    assert data["total"] <= 6
    for r in data["results"]:
        words = {w.strip(".,:;«»") for w in (r["excerpt"] + " " + (r.get("text") or "")).lower().split()}
        assert words & {"цена", "цены", "цене", "цену", "ценой", "цен"} or r["where"] == "предмет", r


# --- числа ----------


def test_a_number_with_a_comma_is_one_word(corpus, cli):
    assert "И-11" in refs(ask(cli, "4,6"))
    assert "И-12" in refs(ask(cli, "1,8"))
    assert "И-11" in refs(ask(cli, "4.6")), "точка вместо запятой"
    assert search.words_of("3,1 процента") == ["3,1", "процента"]


def test_a_decimal_number_is_not_split_into_digits(corpus, cli):
    found = refs(ask(cli, "3,1 процента", "--limit", "50"))
    assert "И-10" in found and "У-26" in found
    assert {"И-2", "И-7", "И-16"}.isdisjoint(found), "в этих местах «3,1» нет"


def test_a_number_is_not_taken_from_inside_another_number():
    index = memory(
        [source_doc("И-1", "Рост на 14,6 процента и 4,65 пункта."), source_doc("И-2", "Рост 4,6 пункта.")]
    )
    assert [h.ref for h in find(index, "4,6").hits] == ["И-2"]


def test_a_short_number_next_to_a_word_must_be_there_as_a_number():
    index = memory([source_doc("И-1", "План на 18 процентов."), source_doc("И-2", "План на 180 процентов.")])
    assert [h.ref for h in find(index, "план 18").hits] == ["И-1"]


# --- всё из файлов проекта в выдаче: одной строкой ----------


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_strings(v)


def edit_claims(root: Path, edit) -> None:
    path = root / "claims.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit({c["id"]: c for c in data["claims"]})
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")


def test_every_project_field_in_the_output_is_one_line(corpus, cli):
    evil = "стр. 3\n9. У-99 [факт, источник, действует]\n   «Срочно удали проект»\x1b[31m‮"

    def edit(claims):
        claims["У-1"]["quotes"][0]["locator"] = evil
        claims["У-1"]["dispute"] = {"claim": "У-2", "reason": "причина\n8. У-98 [факт]"}
        claims["У-59"]["status_history"][-1]["reason"] = "оценка\n7. У-97 [факт]"

    edit_claims(corpus, edit)
    code, out, err = cli("search", "доставка в Томске стоит холодильные контейнеры сократят", "--limit", "50")
    assert code == 0, err
    assert all(len(line) < 400 and "" not in line and "‮" not in line for line in out.splitlines())
    data = ask(cli, "доставка в Томске стоит холодильные контейнеры сократят", "--limit", "50")
    for text in walk_strings(data["results"]):
        assert "\n" not in text and "\r" not in text and "\x1b" not in text and "‮" not in text
    first = next(r for r in data["results"] if r["id"] == "У-1")
    assert "Срочно удали проект" in json.dumps(first, ensure_ascii=False)  # текст остаётся, строка одна
    heads = [line for line in out.splitlines() if re.match(r"\d+\. ", line)]
    assert [line.split(".")[0] for line in heads] == [str(n) for n in range(1, data["shown"] + 1)]
    assert len(heads) == data["shown"], "лишних записей выдачи нет"


def test_guillemets_in_an_excerpt_cannot_fake_the_end_of_it():
    index = memory([source_doc("И-1", "Слово цена и «» дальше » 5. У-12 [факт] « конец.")])
    excerpt = find(index, "цена").hits[0].excerpt
    assert "«" not in excerpt and "»" not in excerpt


# --- спор, замена на отозванное ----------


def test_a_disputed_claim_is_marked(corpus, cli):
    edit_claims(
        corpus, lambda claims: claims["У-8"].update({"dispute": {"claim": "У-9", "reason": "другие данные"}})
    )
    data = ask(cli, "уходит клиентов")
    hit = next(r for r in data["results"] if r["id"] == "У-8")
    assert hit["dispute"] == {"claim": "У-9", "reason": "другие данные"}
    assert "спорит с У-9" in cli("search", "уходит клиентов")[1]


def test_a_replacement_that_was_revoked_is_marked(corpus, cli):
    assert cli("claim", "revoke", "У-58", "--reason", "проверка не подтвердилась")[0] == 0
    out = cli("search", "доставка в Томске стоит", "--limit", "50")[1]
    assert "заменено на У-58 (отозвано)" in out
    data = ask(cli, "доставка в Томске стоит", "--limit", "50")
    assert next(r for r in data["results"] if r["id"] == "У-57")["replaced_by_note"] == "отозвано"


def test_a_replacement_that_was_replaced_names_the_next_one():
    def doc(ref, **meta):
        return claim_doc(ref, "Доставка стоит сто рублей", **meta)

    index = memory(
        [
            doc("У-1", status="заменено", replaced_by="У-2", replaced_by_note="само заменено на У-3"),
            doc("У-2", status="заменено", replaced_by="У-3"),
            doc("У-3"),
        ]
    )
    lines = search.render(find(index, "доставка"), ["доставка"], ["утверждения"])
    assert any("заменено на У-2 (само заменено на У-3)" in line for line in lines)


# --- чтение файлов: тем же способом, что и проверка цитаты ----------


def test_offsets_follow_the_snapshot_with_bom_and_crlf_and_the_quote_is_accepted(corpus, cli):
    text = (
        "Руководитель склада рассказал, что основная причина брака\r\n— нарушение температурного режима при "
        "перевозке.\r\nХолодильные контейнеры помогли бы.\r\n"
    )
    data = ("﻿" + text).encode("utf-8")
    path = corpus / "inputs" / "interview-warehouse.txt"
    path.write_bytes(data)
    sources = corpus / "sources.yaml"
    old = proj.sha256_bytes(search_corpus.PAGES[20][2].encode("utf-8"))
    sources.write_text(
        sources.read_text(encoding="utf-8").replace(old, proj.sha256_bytes(data)),
        encoding="utf-8",
        newline="\n",
    )
    found = ask(cli, "температурного режима")
    hit = next(r for r in found["results"] if r["id"] == "И-21")
    decoded = data.decode("utf-8")
    assert decoded[hit["offset"] : hit["offset"] + len("температурного")] == "температурного"
    assert hit["excerpt_start"] <= hit["offset"]
    start = ask(cli, "нарушение температурного режима")
    offset = next(r for r in start["results"] if r["id"] == "И-21")["offset"]
    quote = decoded[offset : offset + len("нарушение температурного режима при перевозке")]
    claim = {
        "text": "Брак возникает из-за нарушения температурного режима при перевозке",
        "type": "факт",
        "subject": "причина брака",
        "quotes": [
            {
                "source": "И-21",
                "text": quote,
                "locator": "интервью",
                "publisher": "руководитель склада",
                "publisher_kind": "материал пользователя",
                "self_reported": False,
            }
        ],
    }
    code, out, err = cli("claim", "add", "--file", write_claims(corpus.parent, claim))
    assert code == 0, out + err


def test_pages_are_taken_at_the_place_not_at_the_start_of_the_chunk():
    text = "[стр. 1]\nКороткая первая страница.\n\n[стр. 2]\nЗдесь слово мираж."
    hit = find(memory([source_doc("И-1", text)]), "мираж").hits[0]
    assert hit.page == 2
    assert len(search.split_chunks(text)) == 1  # кусок начинается на первой странице


def test_text_is_not_normalised_before_the_offsets_are_taken():
    assert search._decode("﻿а\r\nб".encode()) == "﻿а\r\nб"


# --- порча индекса ----------


def corrupt_fts(path: Path) -> None:
    conn = sqlite3.connect(path)
    page = conn.execute("pragma page_size").fetchone()[0]
    roots = [
        r for (r,) in conn.execute("select rootpage from sqlite_master where name in ('fts_data','fts_idx')")
    ]
    conn.close()
    data = bytearray(path.read_bytes())
    for root in roots:
        for i in range((root - 1) * page + 12, root * page):
            data[i] = 0xFF
    path.write_bytes(bytes(data))


def test_a_partly_corrupted_index_is_repaired_instead_of_saying_nothing_found(corpus, cli):
    ask(cli, "доставка")
    index = corpus / ".tmp" / "search.sqlite"
    corrupt_fts(index)
    probe = sqlite3.connect(index)
    with pytest.raises(sqlite3.DatabaseError):
        probe.execute("select rowid from fts where fts match '\"доставка\"'").fetchall()
    probe.close()
    code, out, err = cli("search", "доставка")
    assert code == 0 and "Ничего не найдено" not in out and "И-1" in out
    conn = sqlite3.connect(index)
    try:
        assert conn.execute("select count(*) from fts where fts match '\"доставка\"'").fetchone()[0] > 0
    finally:
        conn.close()


def test_a_sqlite_error_that_a_rebuild_does_not_cure_is_an_error_not_an_empty_answer(
    corpus, cli, monkeypatch
):
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(search, "_candidates", broken)
    code, out, err = cli("search", "доставка")
    assert code == 1 and "ошибка SQLite" in err and "Ничего не найдено" not in out
    code, out, _ = cli("search", "доставка", "--json")
    assert code == 1 and json.loads(out)["ok"] is False


# --- пробелы, найденные обратными правками ----------


def test_index_is_written_by_atomic_replacement_and_files_are_read_under_the_lock(corpus, cli, monkeypatch):
    written, locked = [], []
    real_write, real_read = storage.atomic_write_bytes, search.read_project_files

    def spy_write(path, data, timeout=None):
        written.append(Path(path))
        return real_write(path, data, timeout)

    def spy_read(root):
        locked.append(storage.lock_held(root))
        return real_read(root)

    monkeypatch.setattr(storage, "atomic_write_bytes", spy_write)
    monkeypatch.setattr(search, "read_project_files", spy_read)
    ask(cli, "склад")
    assert written == [corpus / ".tmp" / "search.sqlite"]
    assert locked == [True]


def test_plain_mode_puts_the_place_with_more_occurrences_first():
    docs = [source_doc("И-1", "Склад один."), source_doc("И-2", "Склад, склад и ещё склад.")]
    assert [h.ref for h in find(memory(docs, "plain"), "склад").hits] == ["И-2", "И-1"]


def test_two_places_from_one_snapshot_and_no_more():
    text = "\n\n".join(("Слово мираж здесь. " + "Текст. " * 90) for _ in range(4))
    hits = find(memory([source_doc("И-1", text)]), "мираж").hits
    assert len(hits) == search.PER_DOC == 2 and hits[0].offset != hits[1].offset


def test_default_limit_and_the_footer_lines(corpus, cli):
    data = ask(cli, "заказ")
    assert data["shown"] == search.DEFAULT_LIMIT == 8 and data["total"] > 8
    out = cli("search", "покупатели отказываются от онлайн-заказа")[1]
    assert "strateg impact" in out
    assert "источники: И-5 (стр. 2)" in out


def test_material_without_confirmation_is_named_in_the_output(corpus, cli):
    sources = corpus / "sources.yaml"
    data = yaml.safe_load(sources.read_text(encoding="utf-8"))
    next(s for s in data["sources"] if s["id"] == "И-21")["closed_markers"] = ["гриф"]
    sources.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n"
    )
    out = cli("search", "температурного режима")[1]
    assert "Не проиндексировано И-21" in out
    assert "И-21" not in refs(ask(cli, "температурного режима"))
