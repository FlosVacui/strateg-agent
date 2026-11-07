"""Разбор команд Bash и PowerShell для хука прав (ADR-0009, ADR-0011).

Разбор ловит явные случаи и **снижает риск, но не гарантирует защиту**: команду можно записать иначе
(переменная вместо имени подкоманды, скрипт вместо `-c`, закодированная команда). Опора границы — то,
что у основного агента нет веб-инструментов, а у исследователя нет чувствительного.

Три вопроса:
- `fetch_command` — команда исследователя целиком совпадает с `uv run strateg fetch '<адрес>'`
  (необязательно с `--region <код>`);
- `network_reason` — в команде основного агента есть сетевая программа или `strateg fetch`;
- `sensitive_mention` — в команде основного агента названы `.env`, `~/.strateg/` или `.tmp/pending/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# --- Список сетевых программ: единственное место. На каждое имя есть тест. ---------------------------------

# Имя команды (без пути и `.exe`, в нижнем регистре). Утилиты, которые ходят в сеть сами по себе.
NETWORK_PROGRAMS: tuple[str, ...] = (
    "curl",
    "wget",
    "invoke-webrequest",
    "iwr",
    "invoke-restmethod",
    "irm",
    "start-bitstransfer",
    "bitsadmin",
    "nc",
    "ncat",
    "netcat",
    "telnet",
    "ssh",
    "scp",
    "sftp",
    "ftp",
    "tftp",
    "aria2c",
    "lynx",
    "w3m",
)

# `certutil` ходит в сеть только с этими ключами.
CERTUTIL_NETWORK_FLAGS: tuple[str, ...] = ("-urlcache", "-verifyctl", "/urlcache", "/verifyctl")

# Интерпретаторы с командой в аргументе (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`).
INLINE_INTERPRETERS = re.compile(
    r"^(?:python[\d.]*|py|pythonw|pypy[\d.]*|node(?:js)?|deno|bun|perl|ruby|php|lua)$"
)

# Что в тексте такой команды считается сетевым кодом. Для `python -c` — модули, для остальных — их аналоги.
INLINE_NETWORK_CODE = re.compile(
    r"urllib|requests|httpx|socket|http\.client|httplib|ftplib|smtplib|telnetlib|imaplib|poplib|xmlrpc|"
    r"aiohttp|urllib3|pycurl|paramiko|websocket|webbrowser|asyncio\.open_connection|"
    r"strateg\.fetch|strateg\.cli|fetchlib|"
    r"XMLHttpRequest|\bfetch\s*\(|\bnet\.connect|require\(\s*['\"](?:https?|net|tls|dgram)['\"]\s*\)|"
    r"from\s+['\"](?:node:)?(?:https?|net|tls)['\"]|"
    r"Net::HTTP|LWP|IO::Socket|open-uri|Net::FTP|curl_init|file_get_contents\s*\(\s*['\"]https?",
    re.IGNORECASE,
)
# Обфускация: так спрятали бы имя модуля. В `-c` агенту такое не нужно.
INLINE_OBFUSCATION = re.compile(
    r"__import__|importlib|\bexec\s*\(|\beval\s*\(|base64|b64decode|\bcompile\s*\("
)

# PowerShell и .NET: длинные имена, которые нельзя спутать с обычным текстом.
POWERSHELL_NETWORK = re.compile(
    r"invoke-webrequest|invoke-restmethod|start-bitstransfer|net\.webclient|system\.net\.|webclient|httpclient|"
    r"downloadstring|downloadfile|downloaddata|uploadstring|uploadfile|"
    r"\binvoke-expression\b|\biex\b|-encodedcommand|\s-enc\s|/dev/tcp/|/dev/udp/",
    re.IGNORECASE,
)

# Команды, которые открывают адрес в браузере.
OPENERS: tuple[str, ...] = (
    "start",
    "start-process",
    "explorer",
    "xdg-open",
    "open",
    "msedge",
    "chrome",
    "firefox",
)
SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"})
INLINE_FLAGS = frozenset({"-c", "-e", "-r", "-E", "--eval", "-command", "-commandwithargs", "/c", "/k"})
WRAPPERS = frozenset({"env", "sudo", "command", "exec", "nohup", "time", "nice", "xargs", "timeout", "call"})
UV_FLAGS_WITH_VALUE = frozenset(
    {"--project", "--directory", "--with", "--python", "-p", "--env-file", "--group", "--extra"}
)
PYTHON_NAME = re.compile(r"^(?:python[\d.]*|py|pythonw|pypy[\d.]*)$")

# --- Команда исследователя --------------------------------------------------------------------------------

_URL_QUOTED = r"'[^'\x00-\x1f\x7f]+'"
_URL_BARE = r"[A-Za-z0-9._~:/%+=@-]+"  # без `?`, `&`, `#`, `;`: их оболочка понимает по-своему
_REGION = r"[A-Za-z0-9][A-Za-z0-9-]{1,15}"
FETCH_TEMPLATE = re.compile(
    rf"uv run strateg fetch (?P<url>{_URL_QUOTED}|{_URL_BARE})(?: --region (?P<region>{_REGION}))?"
)


@dataclass(frozen=True)
class FetchCheck:
    ok: bool
    why: str = ""
    url: str | None = None


def fetch_command(command: str) -> FetchCheck:
    """Команда исследователя: ровно `uv run strateg fetch '<адрес>'` и необязательно `--region <код>`.

    Вне одинарных кавычек нет ни одного из `; | & $( ` > <` и перевода строки: им негде быть в шаблоне.
    Адрес — `http` или `https`.
    """
    text = command.strip()
    m = FETCH_TEMPLATE.fullmatch(text)
    if m is None:
        return FetchCheck(False, "Команда не совпадает с шаблоном `uv run strateg fetch '<адрес>'`")
    raw = m.group("url")
    url = raw[1:-1] if raw.startswith("'") else raw
    try:
        parts = urlsplit(url)
    except ValueError:
        return FetchCheck(False, "Адрес не разбирается")
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return FetchCheck(False, "Адрес должен быть http или https")
    return FetchCheck(True, url=url)


# --- Разбор команды основного агента -----------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    text: str
    words: tuple[str, ...]
    subst: bool  # внутри есть `$(`, обратная кавычка или перенаправление: текст нельзя пропускать


def _split(command: str) -> tuple[list[Segment], bool]:
    """Делит команду по `; & | ( ) { }`, переводу строки и обратным кавычкам вне кавычек.

    Второе значение — разобрано без ошибок (False: кавычка не закрыта, разбор ненадёжен).
    """
    segments: list[Segment] = []
    cur: list[str] = []
    words: list[str] = []
    word: list[str] = []
    in_word = False
    subst = False
    state = None  # None, "'" или '"'
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal in_word
        if in_word:
            words.append("".join(word))
            word.clear()
            in_word = False

    def end_segment() -> None:
        nonlocal subst
        end_word()
        text = "".join(cur).strip()
        if text:
            segments.append(Segment(text, tuple(words), subst))
        cur.clear()
        words.clear()
        subst = False

    while i < n:
        c = command[i]
        if state == "'":
            cur.append(c)
            if c == "'":
                state = None
            else:
                word.append(c)
        elif state == '"':
            cur.append(c)
            if c == "\\" and i + 1 < n and command[i + 1] in '"\\$`':
                i += 1
                cur.append(command[i])
                word.append(command[i])
            elif c == '"':
                state = None
            else:
                if c == "`" or (c == "$" and command[i + 1 : i + 2] == "("):
                    subst = True
                word.append(c)
        else:
            if c in "'\"":
                state = c
                in_word = True
                cur.append(c)
            elif c in ";&|\n\r(){}":
                if c == "(" and cur and cur[-1] == "$":
                    subst = True
                end_segment()
            elif c == "`":
                subst = True
                end_segment()
            elif c in "<>":
                subst = True
                cur.append(c)
                end_word()
            elif c.isspace():
                cur.append(c)
                end_word()
            else:
                cur.append(c)
                word.append(c)
                in_word = True
        i += 1
    end_segment()
    return segments, state is None


def _program(word: str) -> str:
    """Имя программы: без пути и `.exe`, в нижнем регистре."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def strateg_call(words: tuple[str, ...]) -> str | None:
    """Подкоманда `strateg`, если сегмент — вызов CLI (`uv run strateg …`, `python -m strateg.cli …`).

    Возвращает слово после `strateg` (или пустую строку, если его нет); None — это не вызов `strateg`.
    """
    i = 0
    n = len(words)
    while i < n and (re.match(r"^[A-Za-z_]\w*=", words[i]) or _program(words[i]) in WRAPPERS):
        i += 1
    if i < n and _program(words[i]) == "uv":
        i += 1
        if i < n and words[i] == "run":
            i += 1
        while i < n and words[i].startswith("-"):
            i += 2 if words[i] in UV_FLAGS_WITH_VALUE else 1
    if i >= n:
        return None
    prog = _program(words[i])
    if prog == "strateg":
        return words[i + 1] if i + 1 < n else ""
    if PYTHON_NAME.match(prog):
        for j in range(i + 1, n - 1):
            if words[j] == "-m" and _program(words[j + 1]) in ("strateg", "strateg.cli"):
                return words[j + 2] if j + 2 < n else ""
    return None


def _network_word(words: tuple[str, ...]) -> str | None:
    names = NETWORK_PROGRAMS
    for w in words:
        prog = _program(w)
        if prog in names:
            return prog
    for i, w in enumerate(words):
        prog = _program(w)
        if prog == "certutil" and any(x.lower() in CERTUTIL_NETWORK_FLAGS for x in words[i + 1 :]):
            return "certutil -urlcache"
        nxt = words[i + 1].lower() if i + 1 < len(words) else ""
        if prog in OPENERS and nxt.startswith(("http://", "https://", "www.")):
            return f"{prog} <адрес>"
    return None


def _rest(command: str) -> tuple[list[Segment], bool]:
    """Сегменты, которые не вызов `strateg` (их проверяем по тексту), и признак `strateg fetch`.

    Текст аргументов `strateg` (`--reason '…'`, `--answer '…'`) — данные, а не команда: его не просматриваем,
    если в сегменте нет подстановок.
    """
    segments, ok = _split(command)
    if not ok:  # кавычка не закрыта: разбор ненадёжен, смотрим весь текст целиком
        segments = [Segment(command, tuple(command.split()), True)]
    rest: list[Segment] = []
    for seg in segments:
        sub = strateg_call(seg.words)
        if sub is not None:
            if sub.lower() == "fetch":
                return [], True
            if not seg.subst:
                continue
        rest.append(seg)
    return rest, False


def network_reason(command: str, *, _depth: int = 0) -> str | None:
    """Почему команда основного агента — сетевая: что нашли. None — не нашли (это не гарантия защиты)."""
    rest, fetch = _rest(command)
    if fetch:
        return "`strateg fetch`"
    for seg in rest:
        found = _network_word(seg.words)
        if found:
            return found
        # `bash -c '…'`, `powershell -Command "…"`: разбираем вложенную команду
        prog_idx = next((i for i, w in enumerate(seg.words) if _program(w) in SHELL_INTERPRETERS), None)
        if prog_idx is not None and _depth < 3:
            for j in range(prog_idx + 1, len(seg.words)):
                if seg.words[j].lower() in INLINE_FLAGS:
                    found = network_reason(" ".join(seg.words[j + 1 :]), _depth=_depth + 1)
                    if found:
                        return found
                    break
    text = chr(10).join(seg.text for seg in rest)
    if POWERSHELL_NETWORK.search(text):
        return "сетевая команда PowerShell или .NET"
    if any(INLINE_INTERPRETERS.match(_program(w)) for seg in rest for w in seg.words):
        if INLINE_NETWORK_CODE.search(text):
            return "код с сетевым модулем в `python -c` или подобной команде"
        if INLINE_OBFUSCATION.search(text):
            return "код, который прячет имя модуля (`__import__`, `exec`, `base64`)"
    return None


_SENSITIVE_MENTION = re.compile(
    r"(?:^|[\s/\\\"'=:~])(?:\.env(?:\.(?!example(?![\w-]))[\w-]+)?(?![\w.-])|\.strateg(?=[/\\\s\"']|$)|"
    r"\.tmp[/\\]+pending(?=[/\\\s\"']|$))",
    re.IGNORECASE,
)


def sensitive_mention(command: str) -> str | None:
    """В команде названы `.env`, `~/.strateg/` или `.tmp/pending/`: что именно. Ищем по тексту, не по пути."""
    rest, _ = _rest(command)
    for seg in rest:
        m = _SENSITIVE_MENTION.search(seg.text)
        if not m:
            continue
        hit = m.group(0).strip(" /\\\"'=:~" + chr(9)).lower()
        if hit.startswith(".env"):
            return ".env"
        if hit.startswith(".strateg"):
            return "~/.strateg/"
        return ".tmp/pending/"
    return None
