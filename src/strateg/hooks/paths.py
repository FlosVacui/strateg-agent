"""Приведение путей и зоны доступа для хуков прав (ADR-0009, ADR-0011).

Путь сначала приводится к виду «якорь + составляющие», и только потом сверяется с зонами: так не обходятся
`..`, `~`, `%USERPROFILE%`, `$HOME`, обратная косая черта, регистр на Windows и macOS, UNC, завершающие
точки и пробелы и потоки NTFS (`имя:поток`). Символические ссылки проверяются второй раз по настоящему
пути (`realpath`): путь считается разрешённым, только если разрешён и сам, и после раскрытия ссылок.

ОС задаётся полем `os_name` («nt» или «posix»), а не берётся у процесса: разбор — чистые строковые
функции, поэтому обе системы проверяются тестами на любой машине. Файловую систему трогает только
`realpath`, и тесты подменяют его.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

NT, POSIX = "nt", "posix"
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENV_EXAMPLE = ".env.example"
RULE_NAMES = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")
GLOB_CHARS = "*?[{"

_VAR_PATTERNS = (
    re.compile(r"%([^%\s/\\]+)%"),  # %USERPROFILE%
    re.compile(r"\$env:([A-Za-z_]\w*)", re.IGNORECASE),  # $env:USERPROFILE
    re.compile(r"\$\{([A-Za-z_]\w*)\}"),  # ${HOME}
    re.compile(r"\$([A-Za-z_]\w*)"),  # $HOME
)
_HOME_NAMES = ("HOME", "USERPROFILE")


@dataclass(frozen=True)
class PathEnv:
    """Что нужно для приведения пути: ОС, корень продукта, папка проектов, дом и текущая папка."""

    os_name: str
    root: str
    projects: str
    home: str
    cwd: str
    env: Mapping[str, str]
    claude_dir: str | None = None
    scratchpad: str | None = None
    realpath: Callable[[str], str] | None = None
    macos: bool = False

    @property
    def case_insensitive(self) -> bool:
        # Windows и macOS: файловая система не различает регистр (macOS — по умолчанию)
        return self.os_name == NT or self.macos


@dataclass(frozen=True)
class NormPath:
    anchor: str  # «c:», «/», «//сервер/ресурс»
    parts: tuple[str, ...]


def _fold(text: str, penv: PathEnv) -> str:
    return text.lower() if penv.case_insensitive else text


def _lookup(name: str, penv: PathEnv) -> str | None:
    env = penv.env
    if penv.os_name == NT:
        for key, value in env.items():
            if key.lower() == name.lower():
                return value
    elif name in env:
        return env[name]
    if name.upper() in _HOME_NAMES and (penv.os_name == NT or name in _HOME_NAMES):
        return penv.home  # переменной нет в среде хука, но «дом» известен
    return None


def expand(raw: str, penv: PathEnv) -> str | None:
    """Раскрывает `~`, `%NAME%`, `$NAME`, `${NAME}`, `$env:NAME`. Неизвестная переменная — None."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text or "\x00" in text or any(ord(c) < 32 for c in text):
        return None
    if text[0] == "~":
        if len(text) > 1 and text[1] not in "/\\":
            return None  # ~user: чей дом — не знаем
        text = penv.home + text[1:]
    for pattern in _VAR_PATTERNS:
        failed = False

        def sub(m: re.Match[str], _pattern: re.Pattern[str] = pattern) -> str:
            nonlocal failed
            value = _lookup(m.group(1), penv)
            if value is None:
                failed = True
                return m.group(0)
            return value

        text = pattern.sub(sub, text)
        if failed:
            return None
    return text


def _split_anchor(text: str, os_name: str) -> tuple[str | None, str]:
    """(якорь, остаток). Якорь None — путь относительный или «от корня текущего диска»."""
    if os_name == NT:
        m = re.match(r"^//[?.]/UNC/([^/]+)/([^/]+)(?:/(.*))?$", text, re.IGNORECASE)
        if m:
            return f"//{m.group(1)}/{m.group(2)}", m.group(3) or ""
        m = re.match(r"^//[?.]/([A-Za-z]:)(?:/(.*))?$", text)
        if m:
            return m.group(1), m.group(2) or ""
        m = re.match(r"^//([^/]+)(?:/([^/]+))?(?:/(.*))?$", text)
        if m and not text.startswith("///"):
            share = f"/{m.group(2)}" if m.group(2) else ""
            return f"//{m.group(1)}{share}", m.group(3) or ""
        m = re.match(r"^([A-Za-z]:)/(.*)$", text)
        if m:
            return m.group(1), m.group(2)
        if re.match(r"^[A-Za-z]:", text):
            raise ValueError("путь с диском без черты («C:имя») зависит от текущей папки на диске")
        if text.startswith("/"):
            return "", text.lstrip("/")
        return None, text
    if text.startswith("/"):
        return "/", text.lstrip("/")
    return None, text


def _clean(component: str, os_name: str) -> str:
    if os_name != NT:
        return component
    component = component.split(":", 1)[0]  # поток NTFS: имя:поток
    return component.rstrip(". ")  # Windows отбрасывает завершающие точки и пробелы


def _walk(parts: list[str], rest: str, os_name: str) -> list[str]:
    out = list(parts)
    for comp in rest.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            if out:
                out.pop()
            continue
        comp = _clean(comp, os_name)
        if comp:
            out.append(comp)
    return out


def normalize(raw: str, penv: PathEnv, base: NormPath | None = None) -> NormPath | None:
    """Путь в виде «якорь + составляющие» без `.` и `..`. Не вышло привести — None (вызывающий отказывает)."""
    text = expand(raw, penv)
    if text is None:
        return None
    text = text.replace("\\", "/")
    try:
        anchor, rest = _split_anchor(text, penv.os_name)
    except ValueError:
        return None
    if anchor is None:  # относительный: от базы
        if base is None:
            base = normalize(penv.cwd, penv) if penv.cwd != raw else None
        if base is None:
            return None
        return NormPath(base.anchor, tuple(_walk(list(base.parts), rest, penv.os_name)))
    if anchor == "":  # «/путь» в Windows: диск текущей папки
        cur = base or normalize(penv.cwd, penv)
        if cur is None:
            return None
        anchor = cur.anchor
    return NormPath(anchor, tuple(_walk([], rest, penv.os_name)))


def to_str(p: NormPath, os_name: str) -> str:
    body = "/".join(p.parts)
    if os_name == NT:
        if p.anchor.startswith("//"):
            return p.anchor + ("/" + body if body else "")
        return p.anchor + "/" + body
    return "/" + body


def _key(p: NormPath, penv: PathEnv) -> tuple[str, tuple[str, ...]]:
    return _fold(p.anchor, penv), tuple(_fold(x, penv) for x in p.parts)


def is_under(p: NormPath, base: NormPath, penv: PathEnv) -> bool:
    pa, pp = _key(p, penv)
    ba, bp = _key(base, penv)
    return pa == ba and pp[: len(bp)] == bp


def _has_pair(parts: tuple[str, ...], first: str, second: str) -> bool:
    return any(a == first and b == second for a, b in zip(parts, parts[1:], strict=False))


@dataclass(frozen=True)
class Place:
    """Куда указывает путь относительно зон продукта (без учёта регистра, если ОС его не различает)."""

    path: NormPath
    env_file: bool = False  # `.env`, `.env.*` (кроме `.env.example`)
    strateg_home: bool = False  # `~/.strateg/`
    pending: bool = False  # `**/.tmp/pending/**`
    evals: bool = False  # `evals/` в корне продукта
    rules: bool = False  # `AGENTS.md`, `CLAUDE.md`, `.claude/`
    in_root: bool = False
    in_projects: bool = False
    in_claude_dir: bool = False  # `projects/`, `skills/`, `plans/`, `plugins/` рабочей папки Claude Code
    in_scratchpad: bool = False
    in_knowledge: bool = False
    project_id: str | None = None
    project_rel: tuple[str, ...] | None = None  # составляющие после `projects/<id>/`

    @property
    def sensitive(self) -> bool:
        """Закрыто всем ролям: ключи, личные данные, исходник с ПДн, стенд оценки."""
        return self.env_file or self.strateg_home or self.pending or self.evals

    def sensitive_name(self) -> str:
        if self.env_file:
            return ".env"
        if self.strateg_home:
            return "~/.strateg/"
        if self.pending:
            return ".tmp/pending/"
        return "evals/"


CLAUDE_DIR_READABLE = ("projects", "skills", "plans", "plugins")


class Zones:
    """Зоны доступа для одного способа приведения путей: по записи пути или по настоящему пути (realpath)."""

    def __init__(self, penv: PathEnv, *, real: bool) -> None:
        self.penv = penv
        self.real = real
        self.root = self._zone(penv.root)
        self.projects = self._zone(penv.projects)
        self.home = self._zone(penv.home)
        self.claude = self._zone(penv.claude_dir) if penv.claude_dir else None
        self.scratch = self._zone(penv.scratchpad) if penv.scratchpad else None

    def _zone(self, raw: str) -> NormPath | None:
        p = normalize(raw, self.penv, base=NormPath("/", ()))
        if p is None:
            return None
        return self.map(p)

    def map(self, p: NormPath) -> NormPath | None:
        if not self.real:
            return p
        fn = self.penv.realpath
        if fn is None:
            return p
        try:
            resolved = fn(to_str(p, self.penv.os_name))
        except (OSError, ValueError):
            return p
        return normalize(resolved, self.penv, base=NormPath(p.anchor, ())) or p

    @staticmethod
    def _sub(base: NormPath | None, name: str) -> NormPath:
        assert base is not None
        return NormPath(base.anchor, (*base.parts, name))

    def classify(self, p: NormPath) -> Place:
        penv = self.penv
        _, pp = _key(p, penv)
        env_file = any(x == ".env" or (x.startswith(".env.") and x != ENV_EXAMPLE) for x in pp)
        pending = _has_pair(pp, ".tmp", "pending")
        strateg_home = self.home is not None and is_under(p, self._sub(self.home, ".strateg"), penv)
        in_root = self.root is not None and is_under(p, self.root, penv)
        evals = in_root and is_under(p, self._sub(self.root, "evals"), penv)
        rules = False
        if in_root and self.root is not None:
            rel = pp[len(self.root.parts) :]
            rule_names = {_fold(n, penv) for n in RULE_NAMES}
            rules = ".claude" in rel or (bool(rel) and rel[-1] in rule_names)
        in_projects = self.projects is not None and is_under(p, self.projects, penv)
        project_id = None
        project_rel = None
        if in_projects and self.projects is not None:
            rel = pp[len(self.projects.parts) :]
            if rel and PROJECT_ID.match(rel[0]):
                project_id, project_rel = rel[0], tuple(rel[1:])
        in_knowledge = in_root and is_under(p, self._sub(self.root, "knowledge"), penv)
        in_claude = self.claude is not None and any(
            is_under(p, self._sub(self.claude, sub), penv) for sub in CLAUDE_DIR_READABLE
        )
        in_scratch = self.scratch is not None and is_under(p, self.scratch, penv)
        return Place(
            path=p,
            env_file=env_file,
            strateg_home=strateg_home,
            pending=pending,
            evals=evals,
            rules=rules,
            in_root=in_root,
            in_projects=in_projects,
            in_claude_dir=in_claude,
            in_scratchpad=in_scratch,
            in_knowledge=in_knowledge,
            project_id=project_id,
            project_rel=project_rel,
        )


def places(raw: str, penv: PathEnv, *, cwd: str | None = None) -> list[Place] | None:
    """Места, на которые указывает путь: по записи и по настоящему пути (после символических ссылок).

    Первым идёт место по записи. None — путь не удалось привести: вызывающий отказывает.
    """
    base = normalize(cwd or penv.cwd, penv, base=NormPath("/", ()))
    if base is None:
        return None
    lexical = normalize(raw, penv, base=base)
    if lexical is None:
        return None
    out = [Zones(penv, real=False).classify(lexical)]
    if penv.realpath is not None:
        zones = Zones(penv, real=True)
        mapped = zones.map(lexical)
        if mapped is not None:
            out.append(zones.classify(mapped))
    return out


def glob_target(pattern: str, path: str | None, cwd: str) -> str:
    """Откуда начинается поиск Glob: `path` (или текущая папка) плюс неизменная часть шаблона.

    `../../inputs/*` и `C:/Work/x/**` не прячутся за маской: проверяется то, что стоит до первой маски.
    """
    pat = pattern.replace("\\", "/")
    static: list[str] = []
    for seg in pat.split("/"):
        if any(c in seg for c in GLOB_CHARS):
            break
        static.append(seg)
    prefix = "/".join(static)
    if pat.startswith(("/", "~", "%", "$")) or re.match(r"^[A-Za-z]:", pat):
        return prefix or "/"
    base = path or cwd
    return f"{base}/{prefix}" if prefix else base


def default_realpath(raw: str) -> str:
    return os.path.realpath(raw)


def penv_from_system(
    *,
    root: str,
    projects: str,
    cwd: str,
    env: Mapping[str, str],
    scratchpad: str | None = None,
) -> PathEnv:
    """PathEnv для текущего процесса: ОС, домашняя папка, рабочая папка Claude Code, `realpath`."""
    import sys

    home = str(Path.home())
    claude_dir = env.get("CLAUDE_CONFIG_DIR") or str(Path(home) / ".claude")
    return PathEnv(
        os_name=NT if os.name == "nt" else POSIX,
        macos=sys.platform == "darwin",
        root=root,
        projects=projects,
        home=home,
        cwd=cwd,
        env=env,
        claude_dir=claude_dir,
        scratchpad=scratchpad,
        realpath=default_realpath,
    )
