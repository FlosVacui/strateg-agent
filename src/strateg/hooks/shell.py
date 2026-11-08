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
    loose: bool = False  # кавычка не закрыта: где команда, не знаем, просматриваются все слова


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
                if c == ">" and command[i + 1 : i + 2] == "|":  # `>|` — перенаправление, а не конвейер
                    i += 1
                    cur.append("|")
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


_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
LAUNCHERS = frozenset({"uv", "uvx", "pipx", "npx", "poetry", "pdm", "hatch"})  # за ними идёт другая команда


def _command_index(words: tuple[str, ...]) -> int:
    """Номер слова, на котором стоит команда: после присваиваний `X=1` в начале."""
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i]):
        i += 1
    return i


def _program_like(word: str) -> bool:
    """Слово называет программу, которая сама ходит в сеть или запускает чужой код."""
    prog = _program(word)
    return (
        prog in NETWORK_PROGRAMS
        or prog in SHELL_INTERPRETERS
        or prog in OPENERS
        or prog == "certutil"
        or bool(INLINE_INTERPRETERS.match(prog))
    )


def _tail_after_strateg(words: tuple[str, ...], start: int, *, scan: bool) -> tuple[str, ...] | None:
    """Слова после `strateg` в `words[start:]`; `scan=False` — только на первом месте (без `uv run`)."""
    for k in range(start, len(words)):
        prog = _program(words[k])
        if prog == "strateg":
            return words[k + 1 :]
        if PYTHON_NAME.match(prog):
            for j in range(k + 1, len(words) - 1):
                if words[j] == "-m" and _program(words[j + 1]) in ("strateg", "strateg.cli"):
                    return words[j + 2 :]
            return None
        if not scan or _program_like(words[k]):
            return None  # другая программа: `strateg` дальше по строке — её аргумент
    return None


def strateg_tail(words: tuple[str, ...]) -> tuple[str, ...] | None:
    """Слова после `strateg`, если сегмент — вызов CLI (`uv run strateg …`, `python -m strateg.cli …`).

    После `uv run` стоят флаги uv, любые, в том числе неизвестные нам, поэтому слово `strateg` ищется среди
    слов сегмента, а не после списка флагов. Чужая программа раньше него отменяет вызов
    (`uv run curl x strateg`). None — это не вызов `strateg`.
    """
    i = _command_index(words)
    while i < len(words) and _program(words[i]) in WRAPPERS:
        i += 1
        i += _command_index(words[i:])
    if i >= len(words):
        return None
    if _program(words[i]) == "uv":
        rest = words[i + 1 :]
        if "run" not in rest:
            return None
        return _tail_after_strateg(rest, rest.index("run") + 1, scan=True)
    return _tail_after_strateg(words, i, scan=False)


def strateg_call(words: tuple[str, ...]) -> str | None:
    """Подкоманда `strateg`, если сегмент — вызов CLI. Слово после `strateg` (пустая строка, если его нет);
    None — это не вызов `strateg`."""
    tail = strateg_tail(words)
    if tail is None:
        return None
    return tail[0] if tail else ""


def _network_word(words: tuple[str, ...], *, loose: bool = False) -> str | None:
    """Сетевая программа на месте команды сегмента.

    Имя программы среди аргументов (`grep -r curl docs/`) вызовом не считается. Если команду запускает другая
    (`env`, `sudo`, `xargs`, `time`, `uv run` и подобные), просматриваются все слова после неё.
    """
    start = _command_index(words)
    if start >= len(words):
        return None
    first = _program(words[start])
    scan_all = loose or first in WRAPPERS or first in LAUNCHERS
    last = len(words) if scan_all else start + 1
    for w in words[start:last]:
        prog = _program(w)
        if prog in NETWORK_PROGRAMS:
            return prog
    for i in range(start, last):
        prog = _program(words[i])
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
        segments = [Segment(command, tuple(command.split()), True, True)]
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


@dataclass(frozen=True)
class StrategCalls:
    """Из чего состоит команда: только ли из вызовов `strateg` и есть ли среди них `strateg fetch`."""

    only: bool  # каждый сегмент — вызов CLI, без подстановок, перенаправлений и незакрытых кавычек
    fetch: bool  # `strateg fetch` есть в какой-либо части команды
    step: bool = False  # есть `strateg step …`: команда меняет шаг, расход до неё надо отнести к старому шагу
    budget_sets: tuple[tuple[str, ...], ...] = ()  # аргументы каждого `strateg budget set …` (не `check`)


def strateg_calls(command: str) -> StrategCalls:
    """Бюджет (ADR-0010): `uv run strateg …` не платный, кроме `fetch`, и остаётся доступным при исчерпании.

    «Только вызовы» требует, чтобы вся команда была вызовами `strateg`: `uv run strateg status; ls` и
    `uv run strateg status | head` — уже чужая команда. Разбор снижает риск, а не гарантирует защиту (как и
    остальной разбор Bash в этом модуле).
    """
    segments, closed = _split(command)
    if not closed:
        return StrategCalls(False, _rest(command)[1])
    tails = [strateg_tail(seg.words) for seg in segments]
    subs = [None if t is None else (t[0] if t else "") for t in tails]
    fetch = any(sub is not None and sub.lower() == "fetch" for sub in subs)
    only = bool(segments) and all(
        sub is not None and not seg.subst for seg, sub in zip(segments, subs, strict=True)
    )
    step = any(sub is not None and sub.lower() == "step" for sub in subs)
    sets = tuple(
        t[2:]
        for t in tails
        if t is not None and len(t) >= 2 and t[0].lower() == "budget" and t[1].lower() != "check"
    )
    return StrategCalls(only, fetch, step, sets)


def network_reason(command: str, *, _depth: int = 0) -> str | None:
    """Почему команда основного агента — сетевая: что нашли. None — не нашли (это не гарантия защиты)."""
    rest, fetch = _rest(command)
    if fetch:
        return "`strateg fetch`"
    for seg in rest:
        found = _network_word(seg.words, loose=seg.loose)
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
    r"\.tmp[/\\]+pending(?=[/\\\s\"']|$)|evals(?=[/\\\s\"']|$))",
    re.IGNORECASE,
)


def sensitive_mention(command: str, *, evals: bool = True) -> str | None:
    """В команде названы `.env`, `~/.strateg/`, `.tmp/pending/` или `evals/`: что именно.

    Ищем по тексту, не по пути; `evals` — отдельное слово или составляющая пути (`git show HEAD:evals/x`),
    а не часть другого слова. `evals=False` — среда без ролей: стенд оценки там не закрываем.
    """
    rest, _ = _rest(command)
    for seg in rest:
        for m in _SENSITIVE_MENTION.finditer(seg.text):
            hit = m.group(0).strip(" /\\\"'=:~" + chr(9)).lower()
            if hit.startswith(".env"):
                return ".env"
            if hit.startswith(".strateg"):
                return "~/.strateg/"
            if hit.startswith("evals"):
                if evals:
                    return "evals/"
                continue
            return ".tmp/pending/"
    return None


# --- Запись через Bash: куда пишет команда (где это распознаётся) ------------------------------------------

NO_FILE = frozenset(
    {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
        "/dev/fd/1",
        "/dev/fd/2",
        "nul",
        "con",
        "$null",
        "-",
    }
)
CD_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir", "set-location", "sl"})
# Все нефлаговые аргументы — пишутся (создаются, правятся, удаляются). Значение — флаги со значением.
WRITES_ALL_ARGS: dict[str, frozenset[str]] = {
    "tee": frozenset(),
    "mv": frozenset({"-t", "--target-directory"}),
    "rm": frozenset(),
    "rmdir": frozenset(),
    "touch": frozenset({"-d", "-t", "-r", "--date", "--reference"}),
    "mkdir": frozenset({"-m", "--mode"}),
    "unlink": frozenset(),
    "shred": frozenset({"-n", "-s", "--iterations", "--size"}),
    "truncate": frozenset({"-s", "-r", "--size", "--reference"}),
    "md": frozenset(),
    "rd": frozenset(),
}
# Пишется последний нефлаговый аргумент.
WRITES_LAST_ARG: dict[str, frozenset[str]] = {
    "cp": frozenset({"-t", "--target-directory"}),
    "ln": frozenset({"-t", "--target-directory"}),
    "install": frozenset({"-m", "-o", "-g", "--mode", "--owner", "--group"}),
}
WIN_COPY = frozenset({"copy", "xcopy"})  # cmd: `copy a b`, `xcopy a b /y`: пишется последний путь
WIN_FLAG = re.compile(r"^/[A-Za-z?](?::.*)?$")  # `/y`, `/E`, `/D:01-01-2020`; `/tmp` — не флаг
RENAME = frozenset({"ren", "rename"})  # `ren старое новое`: меняется первый путь
GIT_WHOLE_TREE = frozenset({"reset", "clean"})  # затрагивают дерево целиком
GIT_PATHS = frozenset({"checkout", "restore"})  # пути после `--` или все слова
GIT_ALL_ARGS = frozenset({"rm", "mv"})
# PowerShell: имя командлета (и псевдоним) -> путь берётся из -Path/-FilePath/-LiteralPath или первого слова.
PS_WRITES = frozenset(
    {
        "set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "remove-item", "ri", "del",
        "erase", "clear-content", "clc", "set-itemproperty", "tee-object", "rename-item", "rni",
    }
)  # fmt: skip
PS_COPY = frozenset({"copy-item", "cpi"})
PS_MOVE = frozenset({"move-item", "mi"})
PS_PATH_PARAMS = frozenset({"-path", "-filepath", "-literalpath"})
PS_VALUE_PARAMS = frozenset(
    {
        "-value", "-encoding", "-filter", "-include", "-exclude", "-itemtype", "-stream", "-width", "-name",
        "-delimiter", "-inputobject", "-newname", "-credential", "-variable",
    }
)  # fmt: skip
SED_SCRIPT_FLAGS = frozenset({"-e", "-f", "--expression", "--file"})
SED_IN_PLACE = re.compile(r"^-[A-Za-z]*i")  # `-i`, `-i.bak`, `-Ei`, `-ni`: буква `i` среди склеенных флагов


def _tokens(text: str) -> tuple[list[str], list[str]]:
    """Слова сегмента без перенаправлений и цели перенаправлений `>`, `>>`, `>|` (по тексту сегмента)."""
    words: list[str] = []
    targets: list[str] = []
    word: list[str] = []
    in_word = False
    pending: str | None = None  # что делать со следующим словом: "target" или "skip"
    state = None
    i, n = 0, len(text)

    def end_word() -> None:
        nonlocal in_word, pending
        if not in_word:
            return
        value = "".join(word)
        word.clear()
        in_word = False
        if pending == "target":
            targets.append(value)
        elif pending != "skip":
            words.append(value)
        pending = None

    while i < n:
        c = text[i]
        if state:
            if c == state:
                state = None
            else:
                word.append(c)
        elif c in "'\"":
            state = c
            in_word = True
        elif c == ">":
            if in_word and "".join(word).isdigit():  # `2>` и `1>`: номер потока, а не слово
                word.clear()
                in_word = False
            else:
                end_word()
            while i + 1 < n and text[i + 1] in ">|":
                i += 1
            pending = "target"
        elif c == "<":
            end_word()
            while i + 1 < n and text[i + 1] == "<":
                i += 1
            pending = "skip"  # файл или ограничитель heredoc: не цель записи
        elif c.isspace():
            end_word()
        else:
            word.append(c)
            in_word = True
        i += 1
    end_word()
    return words, targets


def _positional(
    args: list[str], value_flags: frozenset[str] = frozenset(), *, slash_flags: bool = False
) -> list[str]:
    """Слова-не-флаги; значение флага из `value_flags` (`touch -d 2020-01-01`) путём не считается."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in value_flags:
            skip = True
        elif a.startswith("-") or (slash_flags and WIN_FLAG.match(a)):
            continue
        else:
            out.append(a)
    return out


def _sed_files(args: list[str]) -> list[str]:
    """Файлы `sed -i`: слова после скрипта; скрипт — первое слово или значения `-e` и `-f`."""
    has_script = False
    positional: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in SED_SCRIPT_FLAGS:
            has_script = skip = True
        elif a.startswith(("-e", "--expression=", "--file=")) and a != "-e":
            has_script = True  # `-es/a/b/`, `--expression=…`
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)
    return positional if has_script else positional[1:]


def _git_targets(args: list[str]) -> list[str]:
    """Пути, которые меняет `git checkout|restore|reset|clean|rm|mv`; без путей — дерево целиком (`.`)."""
    rest = list(args)
    while rest and rest[0].startswith("-"):  # `git -C dir`, `git -c k=v`
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    if not rest:
        return []
    sub, rest = rest[0], rest[1:]
    if sub in GIT_WHOLE_TREE:
        return ["."]
    if sub in GIT_ALL_ARGS:
        return _positional(rest)
    if sub in GIT_PATHS:
        paths = rest[rest.index("--") + 1 :] if "--" in rest else _positional(rest)
        return paths or ["."]
    return []


def _powershell_targets(prog: str, args: list[str]) -> list[str]:
    paths: list[str] = []
    positional: list[str] = []
    destination: str | None = None
    k = 0
    while k < len(args):
        a = args[k]
        low = a.lower()
        if low in PS_PATH_PARAMS and k + 1 < len(args):
            paths.append(args[k + 1])
            k += 1
        elif low == "-destination" and k + 1 < len(args):
            destination = args[k + 1]
            k += 1
        elif low in PS_VALUE_PARAMS:
            k += 1  # значение параметра — не путь
        elif not a.startswith("-"):
            positional.append(a)
        k += 1
    if prog in PS_COPY:
        return [destination] if destination else positional[1:2]
    if prog in PS_MOVE:
        return [*(paths or positional[:1]), *([destination] if destination else positional[1:2])]
    return paths or positional[:1]


def _command_words(seg_text: str) -> tuple[list[str], list[str], int]:
    words, redirects = _tokens(seg_text)
    i = _command_index(tuple(words))
    while i < len(words) and _program(words[i]) in WRAPPERS:
        i += 1 + _command_index(tuple(words[i + 1 :]))
    return words, redirects, i


def changes_directory(command: str) -> bool:
    """В команде есть смена папки (`cd`, `pushd`, `Set-Location`): пути после неё не привести."""
    segments, _ = _split(command)
    for seg in segments:
        words, _, i = _command_words(seg.text)
        if i < len(words) and _program(words[i]) in CD_COMMANDS:
            return True
    return False


def write_targets(command: str) -> list[str]:
    """Пути, в которые команда пишет, как их видно по тексту.

    Перенаправления (`>`, `>>`, `>|`), `tee`, `sed -i` (в том числе `-Ei`), `cp`, `mv`, `rm`, `touch`,
    `mkdir`, `ln`, `dd of=`, `copy`, `xcopy`, `ren`, `git checkout|restore|reset|clean|rm|mv` и командлеты
    PowerShell (`Set-Content`, `Out-File`, `Remove-Item`, `Rename-Item` …).

    Разбор неполный, как и весь разбор Bash: он ловит явные записи и не гарантирует защиту (скрипты
    интерпретаторов, архиваторы, `robocopy`, `git apply` не разбираются). Незакрытая кавычка — не разбираем.
    """
    segments, ok = _split(command)
    if not ok:
        return []
    found: list[str] = []
    for seg in segments:
        words, redirects, i = _command_words(seg.text)
        found += redirects
        if i >= len(words):
            continue
        prog = _program(words[i])
        args = words[i + 1 :]
        if prog in WRITES_ALL_ARGS:
            found += _positional(args, WRITES_ALL_ARGS[prog])
        elif prog in WRITES_LAST_ARG:
            flat = _positional(args, WRITES_LAST_ARG[prog])
            found += flat[-1:] if len(flat) > 1 else []
        elif prog in WIN_COPY:
            flat = _positional(args, slash_flags=True)
            found += flat[-1:] if len(flat) > 1 else []
        elif prog in RENAME:
            found += _positional(args, slash_flags=True)[:1]
        elif prog == "dd":
            found += [a[3:] for a in args if a.startswith("of=")]
        elif prog == "sed":
            if any(a == "--in-place" or a.startswith("--in-place=") or SED_IN_PLACE.match(a) for a in args):
                found += _sed_files(args)
        elif prog == "git":
            found += _git_targets(args)
        elif prog in PS_WRITES | PS_COPY | PS_MOVE:
            found += _powershell_targets(prog, args)
    return [t for t in found if t and t.lower() not in NO_FILE]
