"""Строки, похожие на ключи и пароли: общие шаблоны для упаковки (`pack`) и проверок текста до модели.

Один список шаблонов на двоих: `pack` ищет в файлах проекта и отказывает, а `safety` заменяет найденное
меткой `[ключ]` в тексте снимка и материала, который увидит модель. Значение ключа нигде не
показывается: в сообщения идёт только название вида.
"""

from __future__ import annotations

import re
from typing import NamedTuple

KEY_LABEL = "[ключ]"

# (название для сообщения, шаблон). Если у шаблона есть группа `v`, значение — она, иначе всё совпадение.
# Просмотры вперёд ограничены: на длинной строке из одинаковых знаков перебор не растёт квадратом.
_KEY_BODY = r"[A-Za-z0-9_\-]"
_B64URL = r"[A-Za-z0-9_\-]"
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ключ Anthropic (sk-ant-…)", re.compile(rf"(?<![A-Za-z0-9])sk-ant-{_KEY_BODY}{{16,}}")),
    (
        "ключ вида sk-…",
        re.compile(rf"(?<![A-Za-z0-9])sk-(?=[A-Za-z0-9_\-]{{0,80}}\d){_KEY_BODY}{{20,}}"),
    ),
    ("токен GitHub (ghp_…)", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}")),
    ("токен GitHub (github_pat_…)", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}")),
    ("токен GitLab (glpat-…)", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_\-]{20,}")),
    ("ключ AWS (AKIA…)", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    (
        "токен Slack (xoxb-, xoxp-, xoxa-, xoxr-, xoxs-)",
        re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9\-]{10,}"),
    ),
    ("ключ Google API (AIza…)", re.compile(r"(?<![A-Za-z0-9_\-])AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9_\-])")),
    (
        "токен бота Telegram",
        re.compile(r"(?<![A-Za-z0-9:])\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"),
    ),
    (
        "токен JWT (три части)",
        re.compile(
            rf"(?<![A-Za-z0-9_\-])eyJ{_B64URL}{{8,}}\.{_B64URL}{{8,}}\.{_B64URL}{{8,}}(?![A-Za-z0-9_\-])"
        ),
    ),
    (
        "закрытый ключ (BEGIN … PRIVATE KEY)",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    (
        "строка подключения с паролем (схема://имя:пароль@хост)",
        re.compile(
            r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s:/@\"'<>]{1,64}:(?P<v>[^\s@/\"'<>]{6,128})@[^\s/\"'<>]{1,255}"
        ),
    ),
    (
        "заголовок Authorization: Bearer … с длинным значением",
        re.compile(r"(?<![A-Za-z0-9])Bearer\s+(?=[A-Za-z0-9._~+/=\-]{0,80}\d)[A-Za-z0-9._~+/=\-]{20,}"),
    ),
    (
        "заголовок Authorization: Basic … с длинным значением",
        re.compile(r"(?i:Authorization)\s*:\s*Basic\s+(?P<v>[A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=])"),
    ),
    (
        "строка password= или token= с длинным значением",
        re.compile(
            r"""(?<![A-Za-z0-9])(?:password|passwd|token)["']?\s*[=:]\s*["']?(?P<v>[^\s"',;]{12,})""",
            re.IGNORECASE,
        ),
    ),
)
# Подстановка вместо значения: ${NAME}, <описание>, {{шаблон}}, %NAME%, звёздочки, один повторяющийся знак.
_PLACEHOLDER = re.compile(r"^(?:\$\{?\w+\}?$|<[^>]*>?|\{\{|%\w+%$|\*{4,}|(.)\1{5,}$|x{4,})", re.IGNORECASE)
# Слова, которыми в документации заменяют пароль в строке подключения: не значение.
_WORD_PASSWORDS = frozenset({"password", "passwd", "secret", "changeme", "pass1234", "yourpassword"})


class KeyHit(NamedTuple):
    start: int
    end: int
    kind: str


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.match(value)) or value.lower() in _WORD_PASSWORDS or value == KEY_LABEL


def find_keys(text: str) -> list[KeyHit]:
    """Все места в тексте, похожие на ключ: (начало, конец, вид). Подстановки вида `${TOKEN}` не в счёт.

    Для шаблона с группой `v` место — значение, а не вся строка («password=» остаётся на месте).
    Пересекающиеся места не повторяются: берётся то, что нашлось раньше и длиннее.
    """
    hits: list[KeyHit] = []
    for name, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            if "v" in pattern.groupindex:
                value = m.group("v")
                if _is_placeholder(value):
                    continue
                hits.append(KeyHit(m.start("v"), m.end("v"), name))
            else:
                hits.append(KeyHit(m.start(), m.end(), name))
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    out: list[KeyHit] = []
    for hit in hits:
        if out and hit.start < out[-1].end:
            continue
        out.append(hit)
    return out


def scan_lines(text: str) -> list[tuple[int, str]]:
    """Совпадения шаблонов по строкам: (номер строки, название вида). Одно совпадение на строку и шаблон."""
    hits: list[tuple[int, str]] = []
    for n, line in enumerate(text.split("\n"), start=1):
        for name, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(line):
                value = m.groupdict().get("v")
                if value is not None and _is_placeholder(value):
                    continue  # «${TOKEN}», «<ключ>», «xxxxxxxxxxxx» — подстановка, не значение
                hits.append((n, name))
                break
    return hits


def mask_keys(text: str) -> tuple[str, list[KeyHit]]:
    """Текст с `[ключ]` на месте найденных значений и найденные места (по исходному тексту)."""
    hits = find_keys(text)
    if not hits:
        return text, []
    out: list[str] = []
    pos = 0
    for h in hits:
        out.append(text[pos : h.start])
        out.append(KEY_LABEL)
        pos = h.end
    out.append(text[pos:])
    return "".join(out), hits
