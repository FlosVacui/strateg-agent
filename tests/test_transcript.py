"""Разбор транскрипта и цена сообщения (ADR-0010): повторы, битые строки, чтение с места, таблица цен."""

from __future__ import annotations

import json

import pytest

from strateg import budget, pricing
from strateg import transcript as tr
from transcripts import HAIKU, OPUS, SONNET, append, assistant, searches, user_line, write

NL = chr(10)

# --- разбор -------------------------------------------------------------------------------------------------


def test_parse_takes_only_accounting_fields():
    line = assistant("m1", SONNET, inp=3, out=7, cache_read=11, write_5m=13, write_1h=17, web=2, geo="us")
    parsed = tr.parse(line.encode("utf-8"))
    u = parsed.messages["m1"]
    assert (u.model, u.input_tokens, u.output_tokens, u.cache_read_tokens) == (SONNET, 3, 7, 11)
    assert (u.cache_write_5m_tokens, u.cache_write_1h_tokens, u.web_searches, u.geo) == (13, 17, 2, "us")
    assert parsed.skipped == 0 and parsed.lines == 1


def test_cache_total_without_split_counts_as_five_minutes():
    """У старых версий среды нет разбивки записи кэша: вся сумма — запись на 5 минут."""
    record = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": SONNET,
            "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 50},
        },
    }
    u = tr.parse(json.dumps(record).encode()).messages["m1"]
    assert (u.cache_write_5m_tokens, u.cache_write_1h_tokens) == (50, 0)


def test_the_same_message_is_counted_once_even_when_it_repeats():
    """Сообщение с несколькими блоками пишется несколькими строками с одним id."""
    same = assistant("m1", inp=1, out=100)
    lines = [same, user_line(), same, assistant("m1", inp=1, out=250), assistant("m2", out=5)]
    parsed = tr.parse("\n".join(lines).encode("utf-8"))
    assert list(parsed.messages) == ["m1", "m2"]
    assert parsed.messages["m1"].output_tokens == 250  # выше не бывает меньше: берётся большее


def test_repeat_of_a_message_keeps_the_largest_usage_in_any_order():
    """Вывод растёт по ходу ответа, но устаревшая копия строки не должна уменьшать счёт."""
    for outs in ((10, 250, 100), (250, 100), (100, 250)):
        lines = [assistant("m1", out=n) for n in outs]
        parsed = tr.parse(NL.join(lines).encode("utf-8"))
        assert parsed.messages["m1"].output_tokens == 250, outs


def test_parse_takes_only_web_search_tool_use_ids():
    """Из `content` берутся id блоков `tool_use` с именем `WebSearch`; другие вызовы и текст не читаются."""
    line = assistant(
        "m1", tools=(("t1", "WebSearch"), ("t2", "Read"), ("t3", "WebSearch"), ("t4", "WebFetch"))
    )
    u = tr.parse(line.encode("utf-8")).messages["m1"]
    assert u.search_calls == frozenset({"t1", "t3"})
    assert tr.parse(assistant("m2").encode("utf-8")).messages["m2"].search_calls == frozenset()
    odd = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m3",
                "model": SONNET,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "name": "WebSearch"},  # без id: не отличить повтор
                    {"type": "tool_use", "id": 7, "name": "WebSearch"},
                    {"type": "text", "id": "x", "name": "WebSearch"},
                    "строка вместо блока",
                    {"type": "tool_use", "id": "t9", "name": "WebSearch"},
                ],
            },
        }
    )
    assert tr.parse(odd.encode("utf-8")).messages["m3"].search_calls == frozenset({"t9"})
    broken = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m4",
                "model": SONNET,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": "текст",
            },
        }
    )
    assert tr.parse(broken.encode("utf-8")).messages["m4"].search_calls == frozenset()


def test_web_search_block_is_unique_by_id_across_repeated_lines_and_messages():
    """Сообщение пишется несколькими строками, строка может повториться: поиск считается один раз."""
    lines = [
        assistant("m1", out=5, tools=searches("t1")),
        assistant("m1", out=9, tools=searches("t1", "t2")),  # то же сообщение, дописан второй блок
        assistant("m1", out=9, tools=searches("t1", "t2")),  # дословный повтор строки
        assistant("m2", out=5, tools=searches("t2", "t3")),  # копия блока t2 в другом сообщении
    ]
    parsed = tr.parse(NL.join(lines).encode("utf-8"))
    assert parsed.messages["m1"].search_calls == frozenset({"t1", "t2"})
    assert parsed.messages["m2"].search_calls == frozenset({"t3"})
    total = sum(len(u.search_calls) for u in parsed.messages.values())
    assert total == 3


def test_synthetic_messages_of_the_environment_are_not_counted_as_skipped():
    """Ответ об ошибке API (`<synthetic>`) денег не стоит и повреждением транскрипта не считается."""
    for usage in ({"input_tokens": 0, "output_tokens": 0}, "нет", None):
        message = {"id": "e1", "model": "<synthetic>", "usage": usage}
        line = json.dumps({"type": "assistant", "message": message})
        parsed = tr.parse(NL.join([line, assistant("m1", out=1)]).encode("utf-8"))
        assert list(parsed.messages) == ["m1"] and parsed.skipped == 0, usage


def test_broken_lines_are_skipped_and_counted_and_never_raise():
    good = assistant("m1", out=1)
    data = "\n".join(
        [
            "это не json",  # битая строка
            '{"type": "assistant", "message": {"id": "x", ',  # оборванная посреди объекта
            '{"type": "assistant", "message": {"id": "x", "usage": ',  # то же, но с началом `{`
            good,
            "[1, 2]",
            user_line(),  # чужой тип строки не пропуск: учёту он не нужен
            json.dumps({"type": "api-request", "note": "assistant"}),
        ]
    )
    parsed = tr.parse(data.encode("utf-8"))
    assert list(parsed.messages) == ["m1"]
    assert parsed.skipped == 4


@pytest.mark.parametrize(
    "message",
    [
        {"id": "m1", "model": SONNET},  # нет usage
        {"id": "m1", "model": SONNET, "usage": "много"},
        {"model": SONNET, "usage": {"input_tokens": 1, "output_tokens": 1}},  # нет id
        {"id": "m1", "usage": {"input_tokens": 1, "output_tokens": 1}},  # нет модели
        {"id": "m1", "model": SONNET, "usage": {"output_tokens": 1}},  # нет input_tokens
        {"id": "m1", "model": SONNET, "usage": {"input_tokens": "5", "output_tokens": 1}},
        {"id": "m1", "model": SONNET, "usage": {"input_tokens": -1, "output_tokens": 1}},
        {"id": "m1", "model": SONNET, "usage": {"input_tokens": 1.5, "output_tokens": 1}},
        {"id": "m1", "model": SONNET, "usage": {"input_tokens": True, "output_tokens": 1}},
        {
            "id": "m1",
            "model": SONNET,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation": {"ephemeral_5m_input_tokens": "7"},
            },
        },
    ],
)
def test_message_without_usable_accounting_fields_is_skipped(message):
    line = json.dumps({"type": "assistant", "message": message})
    parsed = tr.parse(line.encode("utf-8"))
    assert parsed.messages == {} and parsed.skipped == 1


def test_assistant_line_without_message_is_skipped():
    parsed = tr.parse(json.dumps({"type": "assistant"}).encode("utf-8"))
    assert parsed.skipped == 1 and parsed.messages == {}


def test_text_of_the_conversation_is_not_kept():
    record = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": SONNET,
            "content": [{"type": "text", "text": "СЕКРЕТНЫЙ ТЕКСТ РАЗГОВОРА"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    parsed = tr.parse(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    assert "СЕКРЕТНЫЙ" not in repr(parsed)


# --- чтение с места -----------------------------------------------------------------------------------------


def test_read_new_continues_from_the_offset(tmp_path):
    path = write(tmp_path / "t.jsonl", assistant("m1", out=1), assistant("m2", out=2))
    first = tr.read_new(path, tr.Cursor())
    assert list(tr.parse(first.data).messages) == ["m1", "m2"] and not first.restarted
    again = tr.read_new(path, first.cursor)
    assert again.data == b"" and again.cursor.offset == first.cursor.offset
    append(path, assistant("m3", out=3))
    third = tr.read_new(path, first.cursor)
    assert list(tr.parse(third.data).messages) == ["m3"]


def test_unfinished_last_line_waits_for_the_next_read(tmp_path):
    whole = assistant("m2", out=2)
    path = write(tmp_path / "t.jsonl", assistant("m1", out=1))
    with path.open("ab") as f:
        f.write(whole[:30].encode("utf-8"))
    first = tr.read_new(path, tr.Cursor())
    assert list(tr.parse(first.data).messages) == ["m1"]
    with path.open("ab") as f:
        f.write((whole[30:] + "\n").encode("utf-8"))
    second = tr.read_new(path, first.cursor)
    assert list(tr.parse(second.data).messages) == ["m2"]


def test_last_line_without_a_newline_is_taken_when_it_is_whole(tmp_path):
    path = write(tmp_path / "t.jsonl", assistant("m1", out=1), assistant("m2", out=2), newline_at_end=False)
    chunk = tr.read_new(path, tr.Cursor())
    assert list(tr.parse(chunk.data).messages) == ["m1", "m2"]
    assert tr.read_new(path, chunk.cursor).data == b""


def test_truncated_file_is_read_from_the_start(tmp_path):
    path = write(tmp_path / "t.jsonl", *[assistant(f"m{i}", out=i) for i in range(1, 6)])
    cursor = tr.read_new(path, tr.Cursor()).cursor
    write(path, assistant("m1", out=1))  # короче прежнего смещения
    chunk = tr.read_new(path, cursor)
    assert chunk.restarted and list(tr.parse(chunk.data).messages) == ["m1"]


def long_line(mid: str, out: int) -> str:
    """Строка настоящего размера: в живых транскриптах строка длиннее отпечатка начала файла (1024 байта)."""
    record = json.loads(assistant(mid, out=out))
    record["message"]["content"] = [{"type": "text", "text": "я" * 2000}]
    return json.dumps(record, ensure_ascii=False)


def test_truncated_file_with_long_lines_is_read_from_the_start_though_its_head_is_unchanged(tmp_path):
    """Обрезка после первых строк: первые 1024 байта прежние, отличить файл можно только по размеру."""
    path = write(tmp_path / "t.jsonl", *[long_line(f"m{i}", i) for i in range(1, 6)])
    cursor = tr.read_new(path, tr.Cursor()).cursor
    assert cursor.head_len == tr.HEAD_BYTES and cursor.offset > 5 * tr.HEAD_BYTES
    write(path, long_line("m1", 1), long_line("m2", 2))  # то же начало, размер меньше смещения
    chunk = tr.read_new(path, cursor)
    assert chunk.restarted and list(tr.parse(chunk.data).messages) == ["m1", "m2"]


def test_rewritten_file_of_the_same_size_is_read_from_the_start(tmp_path):
    path = write(tmp_path / "t.jsonl", assistant("aaaa", out=1), assistant("bbbb", out=2))
    cursor = tr.read_new(path, tr.Cursor()).cursor
    write(path, assistant("cccc", out=1), assistant("dddd", out=2))  # тот же размер, другое начало
    chunk = tr.read_new(path, cursor)
    assert chunk.restarted and list(tr.parse(chunk.data).messages) == ["cccc", "dddd"]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        tr.read_new(tmp_path / "none.jsonl", tr.Cursor())


def test_cursor_from_garbage_starts_over():
    for raw in (None, "x", {"offset": -1}, {"offset": 5}, {"offset": "5", "head_len": 0, "head_sha": ""}):
        assert tr.Cursor.from_dict(raw) == tr.Cursor()


# --- цена сообщения -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def prices() -> budget.Prices:
    return budget.load_prices()


def cost(prices, mid="m", model=SONNET, **kw) -> float | None:
    u = tr.parse(assistant(mid, model, **kw).encode("utf-8")).messages[mid]
    return pricing.message_cost(prices, u)


def test_price_of_all_token_kinds(prices):
    # (1000·2 + 2000·10 + 3000·2,5 + 500·4 + 10000·0,1) / 1e6
    got = cost(prices, inp=1000, out=2000, cache_read=10_000, write_5m=3000, write_1h=500)
    assert got == pytest.approx(0.0325)


def test_web_search_is_priced_by_the_number_of_searches(prices):
    assert cost(prices, web=2) == pytest.approx(0.02)
    assert cost(prices, web=0) == 0
    assert cost(prices, web=None) == 0


def query_cost(prices) -> float:
    """Запрос к модели поиска: средние токены `web_search_query` по ценам Haiku 5.5 из той же таблицы."""
    q = prices.web_search_query
    h = prices.models[q.model]
    return (q.input_tokens * h.input + q.output_tokens * h.output) / 1e6


def test_web_search_query_is_in_the_price_table_with_its_basis(prices):
    q = prices.web_search_query
    assert q.model == HAIKU and q.model in prices.models
    assert (q.input_tokens, q.output_tokens, q.measured) == (11840, 1070, "2026-11-08")
    assert query_cost(prices) == pytest.approx(0.001719)
    text = (budget.config_dir() / "prices.yaml").read_text(encoding="utf-8")
    assert "21 вызов" in text and "248 583" in text  # основание замера записано рядом с числами


def test_web_search_tool_call_costs_the_fee_plus_the_search_model_query(prices):
    one = cost(prices, tools=searches("t1"))
    assert one == pytest.approx(0.01 + 0.001719)
    assert cost(prices, tools=searches("t1", "t2", "t3")) == pytest.approx(3 * (0.01 + 0.001719))
    # токены самого сообщения считаются отдельно от поиска
    assert cost(prices, out=1000, tools=searches("t1")) == pytest.approx(0.01 + 0.01 + 0.001719)
    # вызов другого инструмента поиском не считается
    assert cost(prices, tools=(("t1", "Read"),)) == 0


def test_web_search_query_follows_the_prices_of_its_model(prices):
    """Запрос к модели поиска стоит по её цене из таблицы: сменили модель поиска — изменилась цена."""
    changed = prices.model_copy(
        update={"web_search_query": prices.web_search_query.model_copy(update={"model": OPUS})}
    )
    expected = (11840 * 4 + 1070 * 20) / 1e6
    assert cost(changed, tools=searches("t1")) == pytest.approx(0.01 + expected)


def test_server_reported_searches_are_not_charged_twice(prices):
    """Среда назвала поиски сама (`server_tool_use`): вызовы `WebSearch` того же сообщения их не удваивают."""
    assert cost(prices, web=2, tools=searches("t1", "t2")) == pytest.approx(0.02)
    assert cost(prices, web=1, tools=searches("t1", "t2", "t3")) == pytest.approx(0.01)
    assert cost(prices, web=0, tools=searches("t1")) == pytest.approx(
        0.01 + 0.001719
    )  # ноль — среда не считала


def test_twenty_one_searches_match_the_measured_haiku_bill(prices):
    """Замер: у Haiku 21 поиск, 248 583 токена ввода и 22 529 вывода — $0,2461 вместе с платой за поиск."""
    got = cost(prices, tools=searches(*[f"t{n}" for n in range(21)]))
    measured = 21 * 0.01 + (248_583 * 0.1 + 22_529 * 0.5) / 1e6
    assert measured == pytest.approx(0.2461, abs=5e-5)
    assert got == pytest.approx(measured, rel=0.001)


def test_prices_without_the_search_query_or_with_an_unknown_model_are_refused(tmp_path, monkeypatch):
    import yaml

    from strateg.project import CliError

    raw = yaml.safe_load((budget.config_dir() / "prices.yaml").read_text(encoding="utf-8"))
    for broken in (
        {k: v for k, v in raw.items() if k != "web_search_query"},
        {**raw, "web_search_query": {**raw["web_search_query"], "model": "claude-нет"}},
        {**raw, "web_search_query": {**raw["web_search_query"], "measured": "вчера"}},
        {**raw, "web_search_query": {**raw["web_search_query"], "input_tokens": 0}},
    ):
        folder = tmp_path / f"cfg{id(broken)}"
        folder.mkdir()
        (folder / "prices.yaml").write_text(yaml.safe_dump(broken, allow_unicode=True), encoding="utf-8")
        monkeypatch.setenv(budget.CONFIG_ENV, str(folder))
        with pytest.raises(CliError):
            budget.load_prices()


def test_us_inference_multiplies_tokens_but_not_searches(prices):
    assert cost(prices, out=1000, geo="us") == pytest.approx(0.011)
    assert cost(prices, out=1000, web=1, geo="us") == pytest.approx(0.021)
    assert cost(prices, out=1000, geo="global") == pytest.approx(0.01)


def test_haiku_long_prompt_uses_higher_prices_counting_cache(prices):
    short = cost(prices, model=HAIKU, inp=1000, out=1000, cache_read=50_000)
    assert short == pytest.approx(0.0011)
    long = cost(prices, model=HAIKU, inp=1000, out=1000, cache_read=100_000)  # 101 000 токенов ввода
    assert long == pytest.approx(0.008)
    edge = cost(prices, model=HAIKU, cache_read=100_000)  # ровно порог: цены обычные
    assert edge == pytest.approx(0.001)
    by_write = cost(prices, model=HAIKU, write_5m=100_001)  # порог переходит запись кэша
    assert by_write == pytest.approx(100_001 * 0.625 / 1e6)


def test_fast_mode_uses_fast_prices_and_keeps_cache_shares(prices):
    got = cost(prices, model=OPUS, inp=1000, out=1000, cache_read=1000, speed="fast")
    assert got == pytest.approx((8000 + 40000 + 1000 * 0.4) / 1e6)  # чтение кэша — 0,05 от цены ввода
    assert cost(prices, model=OPUS, inp=1000, out=1000, speed="standard") == pytest.approx(0.024)


def test_unknown_model_has_no_price(prices):
    assert cost(prices, model="claude-future-9") is None


def test_dated_ids_and_context_suffix_find_the_same_prices(prices):
    base = cost(prices, model="claude-sonnet-4-5", out=1000)
    assert base == pytest.approx(0.015)
    assert cost(prices, model="claude-sonnet-4-5-20250929", out=1000) == base
    assert cost(prices, model="claude-opus-5-5[1m]", out=1000) == cost(prices, model=OPUS, out=1000)


def test_current_four_models_match_the_price_page(prices):
    """Четыре текущие модели — как на странице цен (обращение 2026-11-08), USD за миллион токенов."""
    page = {
        "claude-fable-5-1": (10, 12.5, 20, 0.25, 50),
        "claude-opus-5-5": (4, 5, 8, 0.2, 20),
        "claude-sonnet-5-5": (2, 2.5, 4, 0.1, 10),
        "claude-haiku-5-5": (0.1, 0.125, 0.2, 0.01, 0.5),
    }
    for key, (i, w5, w1, r, o) in page.items():
        m = prices.models[key]
        assert (m.input, m.cache_write_5m, m.cache_write_1h, m.cache_read, m.output) == (i, w5, w1, r, o)
    long = prices.models["claude-haiku-5-5"].long_prompt
    assert long is not None and (long.input, long.output, long.threshold_tokens) == (0.5, 2.5, 100_000)


def test_price_table_has_every_selectable_model(prices):
    """Все модели страницы цен, не снятые с API: id как в `Claude API ID`, абсолютные цены."""
    expected = {
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-5-5",
        "claude-haiku-4-5",
    }
    assert set(prices.models) == expected
    assert prices.accessed == "2026-11-08" and prices.web_search_usd_per_1000 == 10
    for key, fast in prices.fast_mode.items():
        assert key in prices.models and fast.output > prices.models[key].output
