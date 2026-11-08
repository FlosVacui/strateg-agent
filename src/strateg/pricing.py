"""Цена одного сообщения по таблице `config/prices.yaml` (ADR-0010): токены × цены за миллион токенов.

Что учитывается:
- ввод, вывод, запись кэша на 5 минут и на час, чтение кэша — по ценам модели;
- у Haiku 5.5 цены выше для запроса, где ввод длиннее порога; в длину входят все токены ввода, в том числе
  чтение и запись кэша (`long_prompt` в таблице);
- веб-поиск — по числу поисков из транскрипта, сверх токенов;
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
    return tokens + u.web_searches * prices.web_search_usd_per_1000 / 1000
