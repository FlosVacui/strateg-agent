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
_ASSIGNMENT_KIND = "строка password=, token=, secret=, api_key= и подобная с длинным значением"
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ключ Anthropic (sk-ant-…)", re.compile(rf"(?<![A-Za-z0-9])sk-ant-{_KEY_BODY}{{16,}}")),
    (
        "ключ вида sk-…",
        re.compile(rf"(?<![A-Za-z0-9])sk-(?=[A-Za-z0-9_\-]{{0,80}}\d){_KEY_BODY}{{20,}}"),
    ),
    ("токен GitHub (ghp_…)", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}")),
    ("токен GitHub (github_pat_…)", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}")),
    ("ключ Stripe (sk_live_…)", re.compile(r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("токен npm (npm_…)", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}")),
    ("токен Hugging Face (hf_…)", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}")),
    (
        "ключ SendGrid (SG.….…)",
        re.compile(r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}(?![A-Za-z0-9_\-])"),
    ),
    (
        "ключ учётной записи Azure (AccountKey=…)",
        re.compile(r"(?<![A-Za-z0-9])AccountKey=(?P<v>[A-Za-z0-9+/]{40,}={0,2})"),
    ),
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
        _ASSIGNMENT_KIND,
        re.compile(
            r"""(?<![A-Za-z0-9])(?:password|passwd|token|secret|api[_-]?key|apikey|client[_-]?secret|"""
            r"""x-api-key|access[_-]?key|private[_-]?key)["']?\s*[=:]\s*["']?(?P<v>[^\s"',;]{12,})""",
            re.IGNORECASE,
        ),
    ),
)
# Подстановка вместо значения: ${NAME}, $NAME, <описание>, {{шаблон}}, %NAME%, звёздочки, один знак подряд,
# «YOUR_API_KEY», «example-token», «changeme».
_PLACEHOLDER = re.compile(
    r"^(?:\$\{?\w+\}?$|\$\(|<[^>]*>?|\{\{|%\w+%$|\*{4,}|(.)\1{5,}$|x{4,}"
    r"|change[_\-]?me[\w\-]*$|(?:your|my|example|sample|placeholder|replace|insert|dummy|fake)[_\-]?\w*[_\-]?"
    r"(?:key|token|secret|password|here|me|value)\w*$)",
    re.IGNORECASE,
)
# Слова, которыми в документации и коде заменяют пароль и пустое значение: не значение.
_WORD_PASSWORDS = frozenset(
    "password passwd secret changeme pass1234 yourpassword none null nil true false undefined".split()
)
# Общий шаблон `name = значение` ловит и код. Это не значение: вызов функции, обращение к элементу или полю
# переменной окружения и настроек, аннотация типа. Настоящее присваивание — литерал: строка или цифры и буквы.
_CALL = re.compile(r"^[A-Za-z_][\w.]*\(")
_SUBSCRIPT = re.compile(r"^[A-Za-z_][\w.]*\[")
_NAME_CHAIN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_NAME_ROOTS = frozenset(
    "process os env environ config settings secrets self this args params options ctx".split()
)


def _is_code_reference(value: str) -> bool:
    """Значение общего шаблона — выражение кода, не ключ: `os.getenv(X)`, `Optional[str]`, `process.env.X`."""
    if _CALL.match(value) or _SUBSCRIPT.match(value):
        return True
    if _NAME_CHAIN.match(value):  # `settings.API_KEY`; точки между кусками с цифрами — уже не имя, а ключ
        return value.split(".", 1)[0] in _NAME_ROOTS or not any(c.isdigit() for c in value)
    return False


# самый короткий ключ из шаблонов (`xoxb-` и десять знаков) — 15 знаков
_MIN_KEY_LINE = 14


# Слова, без которых шаблон не сработает (в нижнем регистре): шаблон запускается, только если одно из них есть
# в тексте. Шаблоны без записи (токен Telegram) запускаются всегда.
_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sk-ant-", ("sk-ant-",)),
    ("sk-…", ("sk-",)),
    ("Stripe", ("sk_", "rk_")),
    ("npm", ("npm_",)),
    ("Hugging", ("hf_",)),
    ("SendGrid", ("sg.",)),
    ("Azure", ("accountkey=",)),
    ("ghp_", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    ("github_pat_", ("github_pat_",)),
    ("GitLab", ("glpat-",)),
    ("AWS", ("akia", "asia")),
    ("Slack", ("xox",)),
    ("Google", ("aiza",)),
    ("JWT", ("eyj",)),
    ("PRIVATE KEY", ("private key",)),
    ("подключения", ("://",)),
    ("Bearer", ("bearer",)),
    ("Basic", ("basic",)),
    ("password=", ("password", "passwd", "token", "secret", "key")),
)


def _needles(name: str) -> tuple[str, ...] | None:
    for fragment, needles in _NEEDLES:
        if fragment in name:
            return needles
    return None


_PATTERN_NEEDLES = tuple(_needles(name) for name, _ in SECRET_PATTERNS)


def _active(text: str) -> list[tuple[str, re.Pattern[str]]]:
    low = text.lower()
    return [
        item
        for item, needles in zip(SECRET_PATTERNS, _PATTERN_NEEDLES, strict=True)
        if needles is None or any(n in low for n in needles)
    ]


class KeyHit(NamedTuple):
    start: int
    end: int
    kind: str


_PREFIX = re.compile(r"^[A-Za-z]{2,}[_-](?:[a-z]+[_-])?")


def _placeholder_body(match: str) -> bool:
    """Токен с префиксом, у которого после префикса почти один знак: `ghp_` и 36 `x`, `sk-` и нули."""
    core = _PREFIX.sub("", match, count=1)
    return len(core) >= 8 and len(set(core.lower())) <= 3


def _is_placeholder(value: str, kind: str = "") -> bool:
    if _PLACEHOLDER.match(value) or value.lower() in _WORD_PASSWORDS or value == KEY_LABEL:
        return True
    return kind == _ASSIGNMENT_KIND and _is_code_reference(value)


def find_keys(text: str) -> list[KeyHit]:
    """Все места в тексте, похожие на ключ: (начало, конец, вид). Подстановки вида `${TOKEN}` не в счёт.

    Для шаблона с группой `v` место — значение, а не вся строка («password=» остаётся на месте).
    Пересекающиеся места не повторяются: берётся то, что нашлось раньше и длиннее.
    """
    hits: list[KeyHit] = []
    for name, pattern in _active(text):
        for m in pattern.finditer(text):
            if "v" in pattern.groupindex:
                value = m.group("v")
                if _is_placeholder(value, name):
                    continue
                hits.append(KeyHit(m.start("v"), m.end("v"), name))
            else:
                if _placeholder_body(m.group()):
                    continue
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
        if len(line) < _MIN_KEY_LINE:  # пустые и короткие строки ключа не несут: миллион строк — доли секунды
            continue
        for name, pattern in _active(line):
            for m in pattern.finditer(line):
                value = m.groupdict().get("v")
                if value is not None and _is_placeholder(value, name):
                    continue  # «${TOKEN}», «<ключ>», «xxxxxxxxxxxx» — подстановка, не значение
                if value is None and _placeholder_body(m.group()):
                    continue
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
