"""Цена одного сообщения по таблице `config/prices.yaml` (ADR-0010): токены × цены за миллион токенов.

Что учитывается:
- ввод, вывод, запись кэша на 5 минут и на час, чтение кэша — по ценам модели;
- у Haiku 5.5 цены выше для запроса, где ввод длиннее порога; в длину входят все токены ввода, в том числе
  чтение и запись кэша (`long_prompt` в таблице);
- веб-поиск — плата за поиск сверх токенов. Число поисков — `server_tool_use.web_search_requests`, а если
  среда его не пишет (нулевое поле), то число уникальных вызовов `WebSearch` в сообщении. К каждому вызову по
  транскрипту прибавляется запрос к модели поиска (`web_search_query` в таблице: средние токены по цене
  этой модели; ключ необязателен — без него вызов стоит одну плату за поиск). Поиски из `server_tool_use`
  платой за запрос не дополняются: тот же вызов не считается дважды;
- ответ только из США (`inference_geo: us`) — цены токенов ×1,1, у Haiku 5.5 и длинные тоже; на поиск
  множитель страница цен не распространяет;
- быстрый режим (`speed: fast`) — цены ввода и вывода из `fast_mode`; запись и чтение кэша считаются от них
  теми же долями, что у обычных цен.

Пакетный режим в транскриптах Claude Code не встречается и не считается. Деньги — числа с плавающей точкой;
округляется только вывод.
"""

from __future__ import annotations

import re

from strateg.budget import ModelPrices, Prices, TokenPrices
from strateg.transcript import Usage

PER = 1_000_000
_DATED = re.compile(r"-\d{8}$")  # claude-sonnet-4-5-20250929 -> claude-sonnet-4-5
_CONTEXT_SUFFIX = re.compile(r"\[\d+[km]\]$", re.IGNORECASE)  # opus[1m]: суффикс окна контекста


def model_key(model: str, prices: Prices) -> str | None:
    """Ключ модели в таблице: точный id, затем без суффикса окна контекста и без даты в конце."""
    name = model.strip()
    candidates = [name]
    without_context = _CONTEXT_SUFFIX.sub("", name)
    candidates += [without_context, _DATED.sub("", without_context)]
    for candidate in candidates:
        if candidate in prices.models:
            return candidate
    return None


def _scaled(base: ModelPrices | TokenPrices, fast_in: float, fast_out: float) -> TokenPrices:
    """Цены быстрого режима: ввод и вывод заданы, кэш — теми же долями от ввода, что у обычных цен."""
    return TokenPrices(
        input=fast_in,
        output=fast_out,
        cache_write_5m=fast_in * base.cache_write_5m / base.input,
        cache_write_1h=fast_in * base.cache_write_1h / base.input,
        cache_read=fast_in * base.cache_read / base.input,
    )


def token_prices(prices: Prices, key: str, u: Usage) -> TokenPrices:
    """Цены за миллион токенов для этого сообщения: длинный запрос Haiku и быстрый режим."""
    base = prices.models[key]
    chosen: TokenPrices = base
    if base.long_prompt is not None:
        prompt = u.input_tokens + u.cache_read_tokens + u.cache_write_5m_tokens + u.cache_write_1h_tokens
        if prompt > base.long_prompt.threshold_tokens:
            chosen = base.long_prompt
    if u.speed == "fast" and key in prices.fast_mode:
        fast = prices.fast_mode[key]
        chosen = _scaled(chosen, fast.input, fast.output)
    return chosen


def web_search_count(u: Usage) -> int:
    """Сколько веб-поисков оплачивает сообщение: по данным среды, а если их нет — по вызовам `WebSearch`."""
    return u.web_searches if u.web_searches > 0 else len(u.search_calls)


def web_search_query_cost(prices: Prices) -> float:
    """Запрос к модели поиска: средние токены `web_search_query` по ценам этой модели из той же таблицы.

    Ключа в таблице нет — запроса нет в цене: поиск стоит одну плату за поиск.
    """
    q = prices.web_search_query
    if q is None:
        return 0.0
    key = model_key(q.model, prices)
    if key is None:  # проверка таблицы (`Prices`) этого не допускает
        raise ValueError(f"модели поиска {q.model} нет в таблице цен")
    probe = Usage(
        message_id="web-search-query", model=key, input_tokens=q.input_tokens, output_tokens=q.output_tokens
    )
    p = token_prices(prices, key, probe)
    return (q.input_tokens * p.input + q.output_tokens * p.output) / PER


def web_search_cost(prices: Prices, u: Usage) -> float:
    """Веб-поиск сообщения: плата за поиск, а у вызовов, которых среда не посчитала, ещё и запрос к модели."""
    fee = prices.web_search_usd_per_1000 / 1000
    if u.web_searches > 0:
        return u.web_searches * fee  # среда назвала поиски сама: их не удваиваем оценкой по вызовам
    return len(u.search_calls) * (fee + web_search_query_cost(prices))


def message_cost(prices: Prices, u: Usage) -> float | None:
    """Стоимость сообщения в долларах; None — модели нет в таблице («нет цены»)."""
    key = model_key(u.model, prices)
    if key is None:
        return None
    p = token_prices(prices, key, u)
    tokens = (
        u.input_tokens * p.input
        + u.output_tokens * p.output
        + u.cache_write_5m_tokens * p.cache_write_5m
        + u.cache_write_1h_tokens * p.cache_write_1h
        + u.cache_read_tokens * p.cache_read
    ) / PER
    if u.geo == "us":
        tokens *= prices.us_data_residency_multiplier  # множитель страницы цен — на токены, не на поиск
    return tokens + web_search_cost(prices, u)
