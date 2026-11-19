"""Стенд оценки: прогон агента на кейсе без человека, с синтетическим руководителем на воротах (ADR-0015).

Что делает прогон:
1. Собирает изолированную копию продукта вне репозитория: только то, что нужно агенту и CLI. Без `evals/`,
   `docs/`, `tests/`, `scripts/`, проектов и `.git`. Строки кейса в копии не встречаются, иначе отказ.
2. Вызывает `claude -p "<первое сообщение>" --output-format json` в копии с явными правами.
3. После каждого вызова читает `strateg status --json` в копии и определяет, чего ждёт агент: ворота Вn,
   внеплановую остановку, ответа на вопрос без ворот или выпуска.
4. Отвечает за руководителя по карточке кейса (без модели, правилами) следующим вызовом
   `claude -p --resume <session_id>`.
5. Заканчивает на выпуске (шаг 7 закрыт) или на пределе вызовов, бюджета или времени.

Итоги — `evals/runs/<кейс>/<время>/`: `result.json` (пишется после каждого вызова), сырые ответы `raw/`, копия
проекта `project/`. Папка `evals/runs/` не попадает в git.

Запуск (нужен вход в claude в профиле, где идёт прогон):
    uv run python evals/run.py fraud-protection --mode автопилот --budget-usd 8 --record
Проверка цикла без сети и без модели, на подменённом claude:
    uv run python evals/run.py fraud-protection --dry-run

Коды выхода: 0 — выпуск; 1 — сбой вызова или прогона (частичные итоги записаны); 2 — `claude` не найден,
кейс или вход не по формату, копия не собралась или в ней нашлись строки кейса; 3 — прогон остановлен
пределом вызовов, бюджета, времени или зацикливанием (итоги записаны, выпуска нет).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVALS = Path(__file__).resolve().parent
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))  # dry_run.py лежит рядом
ROOT = EVALS.parent
CASES = EVALS / "cases"
RUNS = EVALS / "runs"
BENCHMARK = ROOT / "docs" / "benchmark.md"
LEAK_PHRASES_FILE = EVALS / "leak-phrases.yaml"

# Что агенту и CLI нужно в копии. Остальное в копию не попадает.
COPY_FILES = ("AGENTS.md", "CLAUDE.md", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE", ".python-version")
COPY_DIRS = (".claude", "knowledge", "src", "schemas", "config")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "settings.local.json", ".venv")
STUB_README = "Копия продукта для прогона стенда. Описание в репозитории продукта.\n"

PERMISSION_MODE = "acceptEdits"
# По ADR-0015: веб-поиск, чтение и команды `strateg`; запреты по ролям держат хуки проекта
# (в `-p` они исполняются).
# Запись файлов проекта и роли (`Agent`, `Skill`) нужны методике агента. `curl` и прочий `Bash` не разрешены.
ALLOWED_TOOLS = (
    "WebSearch",
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "Agent",
    "Skill",
    "Bash(uv run strateg *)",
    "Bash(uv run --frozen strateg *)",
    "Bash(strateg *)",
)
FORBIDDEN_FLAG = "--dangerously-skip-permissions"

DEFAULT_MAX_CALLS = 40
DEFAULT_MAX_MINUTES = 120.0
DEFAULT_CALL_TIMEOUT_MIN = 30.0
DEFAULT_MAX_TURNS = 200  # предохранитель одного вызова; между воротами агент делает десятки шагов
DEFAULT_MODEL = "sonnet"  # модель основного агента: явно, чтобы профиль не менял цену и поведение
MAX_STALLED = 3  # подряд вызовов без сдвига проекта
CONTINUE_PROMPT = "продолжай"  # ответ, когда вызов упёрся в предел ходов, а ожидания у агента нет

IDLE_ANSWER = "Не знаю, решите сами и запишите допущение."
UNSET_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CLAUDE_PROJECT_DIR")
UNSET_PREFIXES = (
    "STRATEG_",
)  # переменные продукта (STRATEG_HOOKS, STRATEG_DEV и прочие): среда вызова чистая
# Из `CLAUDE*` родителя (`CLAUDE_CODE_*`, `CLAUDE_EFFORT`, `CLAUDECODE` и прочее) в вызов идёт только нужное
# для входа и запуска; плагины, наблюдение за ними, номер сессии родителя — нет: иначе прогоны несравнимы.
KEEP_CLAUDE_ENV = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_GIT_BASH_PATH",
)
SETTING_SOURCES = "project"  # настройки профиля (плагины, навыки профиля, `model`, `env`) в вызов не идут

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LIMIT = 0, 1, 2, 3

Invoker = Callable[..., tuple[int, str, str]]


class StandError(Exception):
    """Стенд не может продолжать: вход не по формату, копия не собралась, строки кейса в копии."""


# ---------------------------------------------------------------------------
# Кейс
# ---------------------------------------------------------------------------


def load_case(case_id: str, cases_dir: Path = CASES) -> dict[str, Any]:
    path = cases_dir / case_id / "card.yaml"
    if not path.is_file():
        raise StandError(f"нет карточки кейса: {path}")
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(card, dict) or not str(card.get("first_message", "")).strip():
        raise StandError(f"в карточке {path} нет первого сообщения (first_message)")
    if not isinstance(card.get("gates"), dict):
        raise StandError(f"в карточке {path} нет ответов на ворота (gates)")
    return card


QUOTED = re.compile(r"«(.+?)»", re.S)
URL = re.compile(r"https?://[^\s)>\]\"'`]+")


def leak_phrases(case_id: str, card: dict[str, Any], cases_dir: Path = CASES) -> list[str]:
    """Строки кейса, которых не должно быть в копии: названия из заголовков и адреса источников.

    Названия берутся из заголовка карточки и ориентиров, адреса — оттуда же; добавочные строки — в
    `evals/leak-phrases.yaml`. Общие слова методики сюда не входят: иначе копия не собралась бы никогда.
    """
    phrases: list[str] = []
    sources = [str(card.get("title", ""))]
    expected = cases_dir / case_id / "expected.md"
    if expected.is_file():
        text = expected.read_text(encoding="utf-8")
        sources.append(next((ln for ln in text.splitlines() if ln.startswith("# ")), ""))
        phrases += [_bare_url(u) for u in URL.findall(text)]
    phrases += [_bare_url(u) for u in URL.findall(yaml.safe_dump(card, allow_unicode=True))]
    for s in sources:
        phrases += QUOTED.findall(s)
    if LEAK_PHRASES_FILE.is_file():
        extra = yaml.safe_load(LEAK_PHRASES_FILE.read_text(encoding="utf-8")) or {}
        phrases += [str(p) for p in extra.get(case_id, [])]
    out: list[str] = []
    for p in phrases:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _bare_url(url: str) -> str:
    return re.sub(r"^https?://", "", url.rstrip(".,;")).rstrip("/")


# ---------------------------------------------------------------------------
# Изолированная копия
# ---------------------------------------------------------------------------


def build_copy(repo: Path, dest: Path) -> None:
    """Копия продукта для прогона: `dest` не должен существовать."""
    if dest.exists():
        raise StandError(f"папка копии уже есть: {dest}")
    dest.mkdir(parents=True)
    for name in COPY_FILES:
        if (repo / name).is_file():
            shutil.copy2(repo / name, dest / name)
    for name in COPY_DIRS:
        if not (repo / name).is_dir():
            raise StandError(f"в репозитории нет папки {name}: копию не собрать")
        shutil.copytree(repo / name, dest / name, ignore=IGNORE, symlinks=False)
    (dest / "README.md").write_text(
        STUB_README, encoding="utf-8", newline="\n"
    )  # pyproject.toml ссылается на него
    (dest / "projects").mkdir()


def find_leaks(copy: Path, phrases: list[str], repo: Path | None = None) -> list[str]:
    """Что в копии выдаёт кейс или репозиторий продукта: строки кейса, ссылки на репозиторий, симлинки."""
    found: list[str] = []
    needles = [(p, _matcher(p)) for p in phrases]
    repo_paths = []
    if repo is not None:
        resolved = str(repo.resolve())
        repo_paths = [resolved, resolved.replace("\\", "/")]
    for path in sorted(copy.rglob("*")):
        rel = path.relative_to(copy).as_posix()
        if path.is_symlink() or path.is_junction():  # junction на Windows — та же ссылка в репозиторий
            found.append(f"{rel}: символическая ссылка")
            continue
        for phrase, match in needles:
            if match(rel):
                found.append(f"{rel}: в имени файла «{phrase}»")
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for phrase, match in needles:
            if match(text):
                found.append(f"{rel}: «{phrase}»")
        for rp in repo_paths:
            if rp in text:
                found.append(f"{rel}: путь к репозиторию продукта")
                break
    return found


def _matcher(phrase: str) -> Callable[[str], bool]:
    if len(phrase) <= 4 and " " not in phrase:  # короткое имя («Ева»): целое слово, с учётом регистра
        pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
        return lambda text: bool(pattern.search(text))
    folded = phrase.casefold()
    return lambda text: folded in text.casefold()


def _inside(path: str | Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def clean_path(path: str, copy: Path | None, repo: Path = ROOT) -> str:
    """`PATH` вызова: каталоги репозитория продукта (его `.venv`) убраны, `.venv` копии — первым.

    Иначе `strateg` в вызове окажется командой репозитория, которая пишет в его `projects/`, а не в копию.
    """
    parts = [p for p in path.split(os.pathsep) if p and not _inside(p, repo)]
    if copy is not None:
        parts.insert(0, str(copy / ".venv" / ("Scripts" if os.name == "nt" else "bin")))
    return os.pathsep.join(parts)


def clean_env(
    extra: dict[str, str] | None = None, *, copy: Path | None = None, repo: Path = ROOT
) -> dict[str, str]:
    """Среда дочерних процессов: без переменных продукта, его окружения и сессии родителя.

    - `STRATEG_*` не передаются вовсе: `STRATEG_HOOKS=0` выключил бы хук прав, барьер к ответам кейса;
    - из `CLAUDE*` остаются только нужные для входа (`KEEP_CLAUDE_ENV`);
    - `PATH`: `.venv` репозитория убран, `.venv` копии первым (`clean_path`).
    """
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        name = k.upper()
        if name in UNSET_ENV or name.startswith(UNSET_PREFIXES):
            continue
        if name.startswith("CLAUDE") and name not in KEEP_CLAUDE_ENV:
            continue
        env[k] = v
    for k in [k for k in env if k.upper() == "PATH"]:
        env[k] = clean_path(env[k], copy, repo)
    if copy is not None and not any(k.upper() == "PATH" for k in env):
        env["PATH"] = clean_path("", copy, repo)
    env.update({"PYTHONUTF8": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    env.update(extra or {})
    return env


def profile_dirs() -> list[Path]:
    """Каталоги профилей Claude: `~/.claude*` и `CLAUDE_CONFIG_DIR`. В них общие память и стенограммы."""
    homes = [p for p in Path.home().glob(".claude*") if p.is_dir()]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        homes.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    return list(dict.fromkeys(homes))


def user_claude_md_excludes() -> list[str]:
    """Пути пользовательского `CLAUDE.md` и правил профиля: в прогоне они агенту не нужны."""
    out: list[str] = []
    for h in profile_dirs() or [Path.home() / ".claude"]:
        base = h.as_posix()
        out += [f"{base}/CLAUDE.md", f"{base}/rules/**"]
    return out


def _worktrees(repo: Path) -> list[Path]:
    """Другие рабочие копии того же репозитория (`git worktree list`); нет git — пусто."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("worktree ")]
    return [Path(ln[len("worktree ") :].strip()) for ln in lines]


def protected_roots(repo: Path, copy: Path | None) -> list[Path]:
    """Где агенту читать и писать нельзя: репозиторий, его рабочие копии, соседние папки с итогами и профили.

    Копия прогона и всё, что её содержит, в список не входит: иначе запрет закрыл бы и саму копию.
    """
    roots = [repo.resolve(), *(w.resolve() for w in _worktrees(repo))]
    parent = repo.resolve().parent
    home = Path.home().resolve()
    if parent != parent.parent and not home.is_relative_to(parent):  # не корень диска и не домашняя папка
        roots.append(parent)  # соседние рабочие копии и итоги прошлых прогонов
    out: list[Path] = []
    for r in roots:
        if copy is not None and copy.resolve().is_relative_to(r):
            continue
        if r not in out:
            out.append(r)
    return [r for r in out if not any(o != r and r.is_relative_to(o) for o in out)]


def permission_globs(path: Path, tail: str = "/**") -> list[str]:
    """Шаблон пути для правил прав: `//` — от корня файловой системы; на Windows диск пишется и как `/c/…`."""
    posix = path.resolve().as_posix().rstrip("/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", posix)
    if m:
        rest = m.group(2) or ""
        return [f"//{m.group(1).lower()}{rest}{tail}", f"//{m.group(1).upper()}:{rest}{tail}"]
    return [f"/{posix}{tail}"]


def deny_rules(repo: Path = ROOT, copy: Path | None = None) -> list[str]:
    """`permissions.deny` вызова: чтение и запись по путям репозитория, рабочих копий и профилей Claude.

    Второй барьер: первый — хук прав продукта в копии.
    """
    rules: list[str] = []
    paths = [(r, "/**") for r in protected_roots(repo, copy)]
    paths += [(p / "projects", "/**") for p in profile_dirs()]  # общие память и стенограммы профилей
    for path, tail in paths:
        for glob in permission_globs(path, tail):
            rules += [f"{tool}({glob})" for tool in ("Read", "Grep", "Glob", "Edit", "Write")]
    return list(dict.fromkeys(rules))


def write_run_settings(path: Path, repo: Path = ROOT, copy: Path | None = None) -> None:
    """Настройки вызова вне копии: автопамять выключена, `CLAUDE.md` профиля исключён, пути закрыты."""
    data = {
        "autoMemoryEnabled": False,
        "claudeMdExcludes": user_claude_md_excludes(),
        "permissions": {"deny": deny_rules(repo, copy)},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# CLI продукта в копии
# ---------------------------------------------------------------------------


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Завершает процесс и всех потомков: через `claude.cmd` внук (`node`) держит каналы и живёт дальше."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tree(
    argv: list[str],
    *,
    input_bytes: bytes | None,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, bytes]:
    """Как `subprocess.run(..., capture_output=True, timeout=…)`, но на тайм-ауте гибнет всё дерево процессов.

    На Windows — `taskkill /T /F`, на POSIX — группа процессов (`start_new_session`).
    """
    extra: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        **extra,
    )
    try:
        out, err = proc.communicate(input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # потомок ушёл из дерева и держит каналы: закрываем свои концы
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except BaseException:  # Ctrl+C и прочее: потомков не оставляем
        kill_tree(proc)
        raise
    return proc.returncode, out, err


class CliError(Exception):
    """`strateg … --json` не прочитан: не запустился, ответ не JSON или с ошибкой."""


NO_PROJECT = ("активный проект не выбран", "проект не найден")  # «проекта ещё нет»: не сбой чтения


class Cli:
    """Команды `strateg` в копии. В настоящем прогоне — `uv run --frozen strateg` в копии."""

    def __init__(self, prefix: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.prefix, self.cwd, self.env = prefix, cwd, env

    def run(self, *args: str, stdin: str | None = None, timeout: float = 300) -> tuple[int, str, str]:
        code, out, err = run_tree(
            [*self.prefix, *args],
            input_bytes=None if stdin is None else stdin.encode("utf-8"),
            cwd=self.cwd,
            env=self.env,
            timeout=timeout,
        )
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def json(self, *args: str) -> dict[str, Any] | None:
        """Ответ команды с `--json`. `None` — проекта ещё нет; не прочитанный ответ — `CliError`."""
        name = f"strateg {args[0]}" if args else "strateg"
        try:
            code, out, err = self.run(*args, "--json")
        except (OSError, subprocess.SubprocessError) as exc:
            raise CliError(f"{name}: не запустился: {exc}") from exc
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise CliError(f"{name}: код {code}, ответ не JSON: {(err or out).strip()[:300]}") from exc
        if not isinstance(data, dict):
            raise CliError(f"{name}: ответ не объект: {out.strip()[:200]}")
        if data.get("ok", True) is False:
            reason = str(data.get("error") or "")
            if any(w in reason for w in NO_PROJECT):
                return None
            raise CliError(f"{name}: {reason or 'ok: false'}")
        if code != 0:
            raise CliError(f"{name}: код {code}: {(err or out).strip()[:300]}")
        return data


def make_cli(copy: Path, *, sync: bool) -> Cli:
    """CLI в копии. С `sync` — своё окружение (`uv sync`). Без него — Python репозитория с `src/` копии."""
    if sync:
        uv = shutil.which("uv")
        if not uv:
            raise StandError("uv не найден в PATH: нужен для окружения копии")
        r = subprocess.run(
            [uv, "sync", "--frozen"],
            cwd=copy,
            env=clean_env(copy=copy),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if r.returncode != 0:
            raise StandError("uv sync в копии не прошёл: " + (r.stderr or r.stdout).strip()[-400:])
        return Cli([uv, "run", "--frozen", "strateg"], copy, clean_env(copy=copy))
    env = clean_env(
        {"PYTHONPATH": str(copy / "src"), "STRATEG_PROJECTS_DIR": str(copy / "projects")}, copy=copy
    )
    code = "import sys; from strateg.cli import main; sys.exit(main())"
    return Cli([sys.executable, "-c", code], copy, env)


# ---------------------------------------------------------------------------
# Ожидание агента
# ---------------------------------------------------------------------------


@dataclass
class Wait:
    """Чего ждёт агент после вызова."""

    kind: str  # «ворота» | «остановка» | «вопрос» | «выпуск»
    gate: str | None = None  # B1…B5 (латиница, как в карточке)


GATE_KEYS = {"В1": "B1", "В2": "B2", "В3": "B3", "В4": "B4", "В5": "B5"}


# Журнал проекта: что двигает ход работы, а что нет. Как у `strateg.project.progress_events`.
STOP_ACTION = "внеплановая остановка"
NOT_PROGRESS_KINDS = ("изменено вне агента",)
HOOK_ACTOR = "хук"
HOOK_PROGRESS_KINDS = ("задание исследователю", "ответ человека")  # события хуков, которые ход всё же двигают
RESUME_NOTE = "начато и не завершено до перерыва; обнаружено при возобновлении работы"


def _journal(project: Path | None) -> list[dict[str, Any]]:
    if project is None or not (project / "journal.jsonl").is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in (project / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return events


def stop_pending(project: Path | None) -> bool:
    """Агент остановился и ждёт ответа: последнее событие хода проекта — «внеплановая остановка».

    `status --json` показывает `paused: остановка` только пока шаг идёт. Агент же закрывает шаг (`step done`)
    и после этого пишет остановку (например, «бюджет израсходован» на границе шагов): шаг закрыт, `paused`
    пуст, а ответа человека агент ждёт. Поэтому остановку стенд читает ещё и по журналу.
    """
    progress = [
        e
        for e in _journal(project)
        if e.get("kind") not in NOT_PROGRESS_KINDS
        and not (e.get("actor") == HOOK_ACTOR and e.get("kind") not in HOOK_PROGRESS_KINDS)
        and not (e.get("status") == "прервано" and e.get("message") == RESUME_NOTE)
    ]
    last = progress[-1] if progress else {}
    return last.get("kind") == "действие" and last.get("name") == STOP_ACTION


def classify_wait(status: dict[str, Any] | None, *, stop_logged: bool = False) -> Wait:
    """По `strateg status --json` и журналу: ворота, остановка, выпуск или вопрос без ворот.

    - ворота ждут ответа — в списке `waiting_gates`;
    - внеплановая остановка — `paused` равно «остановка» (шаг идёт) или `stop_logged`: остановка — последнее
      событие хода проекта (`stop_pending`), а шаг к этому времени уже закрыт;
    - выпуск — шаг 7 закрыт: `step` равен 7 (или 8) и шаг не идёт;
    - иначе агент закончил вызов без записи ожидания: вопрос без ворот. Проекта ещё нет — тоже вопрос.
    """
    if not status:
        return Wait("вопрос")
    waiting = status.get("waiting_gates") or []
    if waiting:
        return Wait("ворота", GATE_KEYS.get(waiting[0], waiting[0]))
    if status.get("paused") == "остановка" or stop_logged:
        return Wait("остановка")
    if str(status.get("step")) in ("7", "8") and not status.get("step_in_progress"):
        return Wait("выпуск")
    return Wait("вопрос")


# ---------------------------------------------------------------------------
# Синтетический руководитель
# ---------------------------------------------------------------------------


@dataclass
class Answer:
    text: str
    source: str  # откуда ответ: поле карточки или «вне карточки»
    off_card: bool = False
    note: str | None = None  # что по карточке выбрать нельзя


def first_quote(text: str) -> str:
    m = QUOTED.search(text)
    return " ".join((m.group(1) if m else text).split())


# Слова причины остановки -> ключ `off_card.stops` в карточке. Слова — из формулировок скиллов и `AGENTS.md`
# («бюджет израсходован», «оценка плана больше остатка бюджета», «не прошли признаки», «признаки закрытых
# данных», «новые сведения противоречат», «причина из пре-мортема»). Порядок важен: первый совпавший;
# «признаки закрытых данных» разбираются раньше «признаков» проверки полноты.
# Ключ — слово; пара слов в кортеже — нужны обе («оценка плана» и «больше остатка»).
STOP_KEYS: list[tuple[str, tuple[tuple[str, ...], ...]]] = [
    ("asked_to_save_page_forbidden_by_robots", (("robots",),)),
    ("premortem_hits_choice", (("пре-мортем",), ("премортем",))),
    ("new_facts_contradict_choice", (("противореч",),)),
    ("personal_or_closed_data_found", (("закрыт",), ("персональн",))),
    ("budget_estimate_over", (("оценка", "больше остатка"), ("оценка", "превыш"), ("оценка", "не уклад"))),
    ("budget_exhausted", (("израсходован",), ("исчерп",), ("бюджет", "не хватит"))),
    (
        "completeness_failed",
        (("полнот",), ("не прошл",), ("не прошёл",), ("признаки:",), ("признаков:",), ("признаки б-",)),
    ),
]
# Причины, для которых в карточке ответа нет: ответ вне карточки, но с пояснением, чего не хватило.
NO_CARD_STOPS: list[tuple[tuple[str, ...], str]] = [
    (("нужен материал",), "нужен материал: ответа в карточке нет"),
    (("материал",), "нужен материал: ответа в карточке нет"),
    (("что задето",), "остановка по задетому при пересмотре: ответа в карточке нет"),
    (("check",), "ошибка проверки реестра: ответа в карточке нет"),
]


class Supervisor:
    """Руководитель по карточке: ответы детерминированные, без второй модели."""

    def __init__(self, card: dict[str, Any], mode: str) -> None:
        self.card, self.mode = card, mode
        self.gap_closures_requested = int((card.get("run_state") or {}).get("gap_closures_requested", 0))

    def answer(self, wait: Wait, project: Path | None, agent_text: str = "") -> Answer:
        if wait.kind == "ворота" and wait.gate:
            return self._gate(wait.gate, project)
        if wait.kind == "остановка":
            return self._stop(project)
        return self._question(agent_text)

    # --- ворота ---

    def _gate(self, gate: str, project: Path | None) -> Answer:
        spec = (self.card.get("gates") or {}).get(gate)
        questions = [q for q in (spec or {}).get("questions", []) if isinstance(q, dict)]
        if not questions:
            return Answer(IDLE_ANSWER, f"gates.{gate}: в карточке нет ответа", True, f"нет ответа на {gate}")
        if gate == "B1":
            return self._b1(questions)
        if gate == "B4":
            return self._b4(questions, project)
        answer = Answer(first_quote(str(questions[0].get("answer", ""))), f"gates.{gate}")
        if len(questions) > 1 and gate == "B3":
            answer.note = (
                "B3: условное правило о закрытии пробела по тексту ворот не применено; дан первый ответ"
            )
        return answer

    def _b1(self, questions: list[dict[str, Any]]) -> Answer:
        lines: list[str] = []
        for q in questions:
            asked = str(q.get("question", ""))
            if asked.startswith("Любой другой") or "вне карточки" in asked:
                continue
            if "Режим" in asked:
                lines.append(self.mode)
            else:
                lines.append(first_quote(str(q.get("answer", ""))))
        return Answer("\n".join(x for x in lines if x), "gates.B1")

    def _b4(self, questions: list[dict[str, Any]], project: Path | None) -> Answer:
        rule = self.card.get("b4_choice_rule") or {}
        options = _read_yaml(project, "options.yaml")
        gaps = _read_yaml(project, "gaps.yaml")
        if not options or not options.get("options"):
            return Answer(
                IDLE_ANSWER,
                "вне карточки: варианты не прочитать",
                True,
                "B4: options.yaml не прочитан, номер варианта по правилу выбрать нельзя",
            )
        if self.gap_closures_requested == 0:  # порядок (а): закрыть пробел открытым источником
            gap = _closable_gap(gaps)
            if gap:
                self.gap_closures_requested += 1
                return Answer(f"сначала закрой {gap}", "gates.B4 (а)")
        active = [
            o for o in options["options"] if o.get("kind") == "активный"
        ]  # активный — любой, кроме «оставить как есть» и «закрыть направление» / «не запускать»
        recommended = (options.get("recommendation") or {}).get("option")
        pick = next((o for o in active if o["id"] == recommended), None)
        if pick is None and active:
            pick = min(active, key=lambda o: _number(o["id"]))
        if pick is None:
            pick = min(options["options"], key=lambda o: _number(o["id"]))
        return Answer(str(_number(pick["id"])), "gates.B4 (б)" if rule else "gates.B4")

    # --- остановка и вопрос ---

    def _stop(self, project: Path | None) -> Answer:
        reason = last_stop_reason(project)
        stops = (self.card.get("off_card") or {}).get("stops") or {}
        key = stop_key(reason)
        if key and key in stops:
            return Answer(str(stops[key]), f"off_card.stops.{key}")
        note = "остановка вне карточки: " + (reason or "причина не записана")
        folded = reason.casefold()
        for words, what in NO_CARD_STOPS:
            if all(w in folded for w in words):
                return self._default(f"{note} ({what})")
        return self._default(note)

    def _question(self, agent_text: str) -> Answer:
        sentences = question_sentences(agent_text)
        unknown = self.card.get("unknown_and_closed") or {}
        for sentence in sentences:
            if any(closed_item_matches(str(item), sentence) for item in unknown.get("items") or []):
                template = unknown.get("answer_template")
                if template:
                    return Answer(str(template), "unknown_and_closed")
        persona = self.card.get("persona") or {}
        for sentence in sentences:
            folded = sentence.casefold()
            if persona.get("deadline") and any(w in folded for w in DEADLINE_WORDS):
                return Answer(f"Срок: {persona['deadline']}.", "persona.deadline")
            if persona.get("reports_to") and any(w in folded for w in RECIPIENT_WORDS):
                return Answer(
                    f"Результат читает: {persona['reports_to']}. Одна страница с рекомендацией.",
                    "persona.reports_to",
                )
        return self._default("вопрос без ворот вне карточки")

    def _default(self, note: str) -> Answer:
        return Answer(IDLE_ANSWER, "вне карточки", True, note)


DEADLINE_WORDS = ("срок", "дедлайн", "к какому числу", "к какому дню", "когда нужно", "когда вам нужно")
RECIPIENT_WORDS = ("кому ", "кому?", "получател", "адресат", "для кого", "кто будет читать", "кто прочитает")


def stop_key(reason: str) -> str | None:
    """Ключ `off_card.stops` по причине остановки; `None` — причины в карточке нет."""
    folded = reason.casefold()
    for key, variants in STOP_KEYS:
        if any(all(w in folded for w in words) for words in variants):
            return key
    return None


def question_sentences(text: str) -> list[str]:
    """Предложения агента, которые заканчиваются знаком вопроса: только в них агент что-то спрашивает."""
    return [m.strip() for m in re.findall(r"[^.!?\n]*\?", text) if len(m.strip()) > 1]


def _stem(word: str) -> str:
    return word[: max(4, len(word) - 2)]


def closed_item_matches(item: str, sentence: str) -> bool:
    """Вопрос про пункт «неизвестно и закрыто»: первое слово пункта (показатель) и ещё одно значимое слово.

    Пункт из одного слова до запятой или тире («конверсия — из просмотра…») узнаётся по этому слову. У пункта
    из нескольких слов нужно второе: одно «результаты» или «планы» в вопросе пункт не называет. При сомнении
    пункт не совпал: ответ идёт как вне карточки.
    """
    words = [w for w in re.findall(r"[а-яёa-z\-]+", item.casefold()) if len(w) >= 4]
    if not words:
        return False
    folded = sentence.casefold()
    if _stem(words[0]) not in folded:
        return False
    head = re.split(r"[—,;:]", item, maxsplit=1)[0]
    head_words = [w for w in re.findall(r"[а-яёa-z\-]+", head.casefold()) if len(w) >= 4]
    if len(head_words) <= 1:
        return True
    return any(_stem(w) in folded for w in words[1:])


def _number(option_id: str) -> int:
    m = re.search(r"(\d+)$", str(option_id))
    return int(m.group(1)) if m else 0


def _closable_gap(gaps: dict[str, Any] | None) -> str | None:
    """Пробел, влияющий на выбор, который закрыть открытым источником не дороже $1 и 10 минут."""
    found: list[tuple[int, str]] = []
    for g in (gaps or {}).get("gaps", []):
        cost = g.get("close_cost") or {}
        usd, minutes = cost.get("usd"), cost.get("minutes")
        if (
            g.get("affects_choice")
            and g.get("status") == "открыт"
            and g.get("close_via") == "открытый источник"
            and usd is not None
            and minutes is not None
            and usd <= 1
            and minutes <= 10
        ):
            found.append((_number(g["id"]), g["id"]))
    return min(found)[1] if found else None


def _read_yaml(project: Path | None, name: str) -> dict[str, Any] | None:
    if project is None or not (project / name).is_file():
        return None
    try:
        data = yaml.safe_load((project / name).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def last_stop_reason(project: Path | None) -> str:
    if project is None or not (project / "journal.jsonl").is_file():
        return ""
    reason = ""
    for line in (project / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "действие" and e.get("name") == "внеплановая остановка":
            reason = str(e.get("message") or "")
    return reason


# ---------------------------------------------------------------------------
# Вызов claude
# ---------------------------------------------------------------------------


@dataclass
class CallOptions:
    cwd: Path
    env: dict[str, str]
    settings: Path
    timeout_s: float
    model: str | None = None
    max_turns: int | None = DEFAULT_MAX_TURNS
    max_budget_usd: float | None = None
    allowed_tools: tuple[str, ...] = ALLOWED_TOOLS
    permission_mode: str = PERMISSION_MODE
    setting_sources: str = SETTING_SOURCES
    mcp_config: Path | None = None  # `.mcp.json` копии, если он есть: других MCP-серверов в вызове нет


def build_argv(exe: str, opts: CallOptions, session_id: str | None) -> list[str]:
    """Аргументы `claude -p`; сообщение — по stdin: через `claude.cmd` многострочный аргумент не пройдёт."""
    argv = [exe, "-p", "--output-format", "json"]
    argv += ["--permission-mode", opts.permission_mode, "--allowedTools", ",".join(opts.allowed_tools)]
    argv += ["--settings", str(opts.settings), "--setting-sources", opts.setting_sources]
    argv += ["--strict-mcp-config"]
    if opts.mcp_config is not None:
        argv += ["--mcp-config", str(opts.mcp_config)]
    if opts.model:
        argv += ["--model", opts.model]
    if opts.max_turns:
        argv += ["--max-turns", str(opts.max_turns)]
    if opts.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max(opts.max_budget_usd, 0.01):.2f}"]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def claude_process(exe: str) -> Invoker:
    """Настоящий вызов. Подменённый — любой другой вызываемый объект того же вида."""

    def invoke(prompt: str, session_id: str | None, opts: CallOptions) -> tuple[int, str, str]:
        code, out, err = run_tree(
            build_argv(exe, opts, session_id),
            input_bytes=prompt.encode("utf-8"),
            cwd=opts.cwd,
            env=opts.env,
            timeout=opts.timeout_s,
        )
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    return invoke


def claude_version(exe: str, env: dict[str, str], cwd: Path) -> str | None:
    """Версия `claude` для итогов (`claude --version`); не удалось — `None`."""
    try:
        code, out, _ = run_tree([exe, "--version"], input_bytes=None, cwd=cwd, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.decode("utf-8", "replace").strip() or None if code == 0 else None


@dataclass
class Reply:
    session_id: str | None
    result: str
    total_cost_usd: float | None
    model_usage: dict[str, Any] | None
    is_error: bool
    subtype: str
    num_turns: int | None = None


def parse_reply(stdout: str) -> Reply:
    """Ответ `--output-format json`: объект или (с `--verbose`) массив событий с итоговым."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ответ не JSON") from exc
    if isinstance(data, list):
        data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), None)
    if not isinstance(data, dict):
        raise ValueError("в ответе нет итога вызова")
    cost = data.get("total_cost_usd")
    usage = data.get("modelUsage")
    return Reply(
        session_id=data.get("session_id"),
        result=str(data.get("result") or ""),
        total_cost_usd=float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
        model_usage=usage if isinstance(usage, dict) else None,
        is_error=bool(data.get("is_error")),
        subtype=str(data.get("subtype") or ""),
        num_turns=data.get("num_turns") if isinstance(data.get("num_turns"), int) else None,
    )


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------


@dataclass
class Limits:
    max_calls: int = DEFAULT_MAX_CALLS
    budget_usd: float | None = None
    max_minutes: float = DEFAULT_MAX_MINUTES
    call_timeout_min: float = DEFAULT_CALL_TIMEOUT_MIN
    max_turns: int | None = DEFAULT_MAX_TURNS


@dataclass
class RunState:
    """Всё, что попадает в `result.json`."""

    case: str
    mode: str
    model: str | None
    dry_run: bool
    started_at: str
    limits: Limits
    claude_version: str | None = None  # `claude --version`
    config_dir: str | None = None  # профиль, в котором шёл прогон (`CLAUDE_CONFIG_DIR` или `~/.claude`)
    product_commit: str | None = None
    product_dirty: bool | None = None  # в рабочей копии продукта были незафиксированные правки
    actual_models: list[str] = field(default_factory=list)  # из `modelUsage`, по убыванию стоимости
    turn_limit_continuations: int = 0  # сколько раз вызов упёрся в `--max-turns`, а стенд ответил «продолжай»
    outcome: str = (
        "идёт"  # идёт | выпуск | предел вызовов | предел бюджета | предел времени | зацикливание | сбой
    )
    failure: str | None = None
    calls: int = 0
    session_ids: list[str] = field(default_factory=list)
    cumulative_costs: list[float | None] = field(default_factory=list)
    total_cost_usd: float | None = None
    model_usage: dict[str, Any] | None = None
    gates: list[dict[str, Any]] = field(default_factory=list)
    stops: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    off_card: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_wait: str | None = None
    finished_at: str | None = None
    clock_seconds: float = 0.0
    project_id: str | None = None
    project_path: str | None = None
    copy_path: str | None = None
    leak_check: dict[str, Any] = field(default_factory=dict)
    final: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["limits"] = dict(self.limits.__dict__)
        d["interrupted"] = self.final.get("interrupted", 0)
        d["aborted"] = self.outcome not in ("выпуск", "идёт")
        d["off_card_answers"] = len(self.off_card)
        return d


def now() -> dt.datetime:
    """Время в поясе машины, как у проекта (`strateg.project.now`)."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def active_project(copy: Path) -> Path | None:
    active = copy / "projects" / ".active"
    if not active.is_file():
        return None
    folder = copy / "projects" / active.read_text(encoding="utf-8").strip()
    return folder if (folder / "state.yaml").is_file() else None


def place_materials(card: dict[str, Any], case_dir: Path, project: Path | None, placed: set[str]) -> None:
    """Файлы кейса — в `inputs/` проекта, как положил бы человек; один раз, когда проект уже есть."""
    if project is None:
        return
    for name in (card.get("materials") or {}).get("files") or []:
        if str(name) in placed:
            continue
        src = case_dir / str(name)
        if not src.is_file():
            raise StandError(f"в карточке указан материал, которого нет: {src}")
        (project / "inputs").mkdir(exist_ok=True)
        shutil.copy2(src, project / "inputs" / src.name)
        placed.add(str(name))


def progress_mark(project: Path | None, status: dict[str, Any] | None) -> tuple[Any, ...]:
    """Отпечаток хода проекта: число событий журнала, шаг и ворота. Не изменился — вызов ничего не сдвинул."""
    lines = 0
    if project and (project / "journal.jsonl").is_file():
        lines = len((project / "journal.jsonl").read_text(encoding="utf-8").splitlines())
    gates = tuple(sorted((status or {}).get("gates", {}).items()))
    return (lines, (status or {}).get("step"), gates)


class Bench:
    """Один прогон: копия, цикл вызовов, итоги."""

    def __init__(
        self,
        case_id: str,
        card: dict[str, Any],
        *,
        mode: str,
        cli: Cli,
        copy: Path,
        invoker: Invoker,
        out: Path,
        limits: Limits,
        settings: Path,
        model: str | None = None,
        dry_run: bool = False,
        case_dir: Path | None = None,
        clock: Callable[[], dt.datetime] = now,
    ) -> None:
        self.card, self.cli, self.copy, self.invoker, self.out = card, cli, copy, invoker, out
        self.settings, self.case_dir, self.clock = settings, case_dir or CASES / case_id, clock
        self.supervisor = Supervisor(card, mode)
        self.started = clock()
        self.state = RunState(
            case_id, mode, model, dry_run, self.started.isoformat(), limits, copy_path=str(copy)
        )
        self.placed: set[str] = set()
        self.stalled = 0
        self.last_mark: tuple[Any, ...] | None = None

    # --- итоги ---

    def write(self) -> None:
        """Итоги после каждого вызова: `result.json`, копия проекта. Сырые ответы пишутся при вызове."""
        st = self.state
        st.finished_at = self.clock().isoformat()
        st.clock_seconds = round((self.clock() - self.started).total_seconds(), 1)
        project = active_project(self.copy)
        if project is not None:
            st.project_id, st.project_path = project.name, str(self.out / "project")
            target = self.out / "project"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(project, target)
            st.final = self.collect_final(project)
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.out / "result.json.tmp"
        tmp.write_text(
            json.dumps(st.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        tmp.replace(self.out / "result.json")

    def collect_final(self, project: Path) -> dict[str, Any]:
        """Числа из проекта: время работы агента, итог `strateg check` и проверки полноты."""
        try:
            status = self.cli.json("status") or {}
        except CliError as exc:
            status = {}
            note = f"итоги: {exc}"
            if note not in self.state.notes:
                self.state.notes.append(note)
        code, out, err = self._check()
        done: dict[str, Any] = {
            "step": status.get("step"),
            "work_minutes": status.get("work_minutes"),
            "clock_minutes": status.get("clock_minutes"),
            "project_cost_usd": status.get("cost_usd"),
            "project_cost_precision": status.get("cost_precision"),
            "interrupted": _count_interrupted(project),
            "check": {"ok": code == 0, "output": (out or err).strip()[:2000]},
            "completeness": _completeness(project),
        }
        return done

    def _check(self) -> tuple[int, str, str]:
        try:
            return self.cli.run("check")
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", f"strateg check не запустился: {exc}"

    # --- цикл ---

    def run(self, first_message: str) -> int:
        st = self.state
        prompt, session, last_text = first_message, None, ""
        while True:
            stop = self._limit_hit()
            if stop:
                st.outcome = stop
                break
            try:
                reply = self._call(prompt, session)
            except _CallFailed as exc:
                st.outcome, st.failure = "сбой", str(exc)
                break
            if reply.subtype == "error_max_budget_usd":
                st.outcome = "предел бюджета"
                break
            session, last_text = reply.session_id or session, reply.result
            project = active_project(self.copy)
            place_materials(self.card, self.case_dir, project, self.placed)
            try:
                status = self.cli.json("status")
            except CliError as exc:  # не прочитанный `status` — не «вопрос»: ответ на ворота ушёл бы не туда
                st.outcome, st.failure = "сбой", f"вызов {st.calls}: {exc}"
                break
            wait = classify_wait(status, stop_logged=stop_pending(project))
            st.last_wait = wait.kind if not wait.gate else f"{wait.kind} {wait.gate}"
            if wait.kind == "выпуск":
                st.outcome = "выпуск"
                break
            mark = progress_mark(project, status)
            self.stalled = self.stalled + 1 if mark == self.last_mark else 0
            self.last_mark = mark
            if self.stalled >= MAX_STALLED:
                st.outcome = "зацикливание"
                st.failure = f"{MAX_STALLED} вызова подряд без изменений в проекте"
                break
            if reply.subtype == "error_max_turns" and wait.kind == "вопрос":
                # агент не закончил, а не спросил: ждать от него нечего, сессия продолжается
                st.turn_limit_continuations += 1
                note = f"вызов упёрся в --max-turns: агент не закончил, ответ «{CONTINUE_PROMPT}»"
                if note not in st.notes:
                    st.notes.append(note)
                prompt = CONTINUE_PROMPT
            else:
                answer = self.supervisor.answer(wait, project, last_text)
                self._record_answer(wait, answer)
                prompt = answer.text
            self.write()
        self.write()
        return {"выпуск": EXIT_OK, "сбой": EXIT_FAILED}.get(st.outcome, EXIT_LIMIT)

    def _limit_hit(self) -> str | None:
        st, lim = self.state, self.state.limits
        if st.calls >= lim.max_calls:
            return "предел вызовов"
        if lim.budget_usd is not None and (st.total_cost_usd or 0) >= lim.budget_usd:
            return "предел бюджета"
        if (self.clock() - self.started).total_seconds() >= lim.max_minutes * 60:
            return "предел времени"
        return None

    def _call(self, prompt: str, session: str | None) -> Reply:
        st, lim = self.state, self.state.limits
        remaining_s = lim.max_minutes * 60 - (self.clock() - self.started).total_seconds()
        remaining_usd = None if lim.budget_usd is None else lim.budget_usd - (st.total_cost_usd or 0)
        mcp = self.copy / ".mcp.json"
        opts = CallOptions(
            cwd=self.copy,
            env=clean_env(copy=self.copy),
            settings=self.settings,
            timeout_s=max(min(lim.call_timeout_min * 60, remaining_s), 1.0),
            model=st.model,
            max_turns=lim.max_turns,
            max_budget_usd=remaining_usd,
            mcp_config=mcp if mcp.is_file() else None,
        )
        n = st.calls + 1
        raw = self.out / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        try:
            code, out, err = self.invoker(prompt, session, opts)
        except subprocess.TimeoutExpired as exc:
            st.calls = n
            raise _CallFailed(f"вызов {n}: тайм-аут {exc.timeout:.0f} с") from exc
        except OSError as exc:
            st.calls = n
            raise _CallFailed(f"вызов {n}: claude не запустился: {exc}") from exc
        st.calls = n
        (raw / f"call-{n:02d}.json").write_text(out, encoding="utf-8", newline="\n")
        if err.strip():
            (raw / f"call-{n:02d}.stderr.txt").write_text(err, encoding="utf-8", newline="\n")
        # Ответ разбирается до проверки кода: `claude -p` с исчерпанным бюджетом или ходами отвечает кодом 1,
        # но с итогом вызова. Стоимость и `modelUsage` берутся из него и у упавшего вызова.
        reply: Reply | None = None
        parse_error: ValueError | None = None
        try:
            reply = parse_reply(out)
        except ValueError as exc:
            parse_error = exc
        if reply is not None:
            if reply.session_id and reply.session_id not in st.session_ids:
                st.session_ids.append(reply.session_id)
            self._take_cost(reply)
            if reply.subtype in LIMIT_SUBTYPES:
                return reply  # предел бюджета и предел ходов — не сбой
        if code != 0:
            raise _CallFailed(f"вызов {n}: код {code}: {err.strip()[:300] or out.strip()[:300]}")
        if reply is None:
            raise _CallFailed(f"вызов {n}: {parse_error}: {out.strip()[:200]}")
        if reply.is_error or reply.subtype.startswith("error"):
            raise _CallFailed(f"вызов {n}: is_error {reply.subtype}: {reply.result[:300]}".strip())
        return reply

    def _take_cost(self, reply: Reply) -> None:
        """Стоимость — последнее накопительное значение, не сумма: итог `--resume` накопительный.

        Значение меньше прежнего накопительным не считается: остаётся максимум, в `notes` запись.
        """
        st = self.state
        st.cumulative_costs.append(reply.total_cost_usd)
        if reply.total_cost_usd is not None:
            previous = st.total_cost_usd
            if previous is not None and reply.total_cost_usd < previous:
                st.notes.append(
                    f"вызов {st.calls}: стоимость уменьшилась ({previous} -> {reply.total_cost_usd}): "
                    "значение не накопительное; оставлен максимум"
                )
            else:
                st.total_cost_usd = reply.total_cost_usd
        if reply.model_usage is not None:
            st.model_usage = reply.model_usage
            st.actual_models = models_by_cost(reply.model_usage)
        elif reply.total_cost_usd is None:
            st.notes.append(f"вызов {st.calls}: в ответе нет стоимости")

    def _record_answer(self, wait: Wait, answer: Answer) -> None:
        st = self.state
        entry = {"call": st.calls, "answer": answer.text, "source": answer.source}
        if wait.kind == "ворота":
            st.gates.append({**entry, "gate": wait.gate})
        elif wait.kind == "остановка":
            st.stops.append({**entry, "reason": last_stop_reason(active_project(self.copy))})
        else:
            st.questions.append(entry)
        if answer.off_card:
            st.off_card.append({**entry, "wait": wait.kind, "note": answer.note})
        if answer.note and answer.note not in st.notes:
            st.notes.append(answer.note)


class _CallFailed(Exception):
    pass


LIMIT_SUBTYPES = ("error_max_budget_usd", "error_max_turns")


def models_by_cost(usage: dict[str, Any]) -> list[str]:
    """Модели из `modelUsage`: по убыванию стоимости (основной агент и субагенты)."""

    def cost(item: Any) -> float:
        v = item.get("costUSD") if isinstance(item, dict) else None
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0

    return [k for k, _ in sorted(usage.items(), key=lambda kv: -cost(kv[1]))]


def _count_interrupted(project: Path) -> int:
    path = project / "journal.jsonl"
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("status") == "прервано":
            n += 1
    return n


def _completeness(project: Path) -> dict[str, Any] | None:
    data = _read_yaml(project, "completeness.yaml")
    if not data:
        return None
    return {k: data.get(k) for k in ("step", "by", "result", "failed", "critic_call", "checked_at")}


# ---------------------------------------------------------------------------
# Запись в docs/benchmark.md
# ---------------------------------------------------------------------------

RECORD_HEADER = (
    "| Дата | Кейс | Режим | Модель | Вызовов | По часам, мин | Работа агента, мин | Стоимость, USD | "
    "Ворота | Остановки | Вне карточки | `strateg check` | Полнота | Итог |"
)
RECORD_SECTION = "## Прогоны стенда"


def record_row(result: dict[str, Any]) -> str:
    final = result.get("final") or {}
    comp = final.get("completeness") or {}
    cost = result.get("total_cost_usd")

    def num(v: Any, fmt: str = "{:.1f}") -> str:
        return "нет данных" if v is None else fmt.format(v)

    cells = [
        str(result["started_at"])[:10],
        result["case"],
        result["mode"],
        result.get("model") or "по умолчанию",
        str(result["calls"]),
        num(result["clock_seconds"] / 60),
        num(final.get("work_minutes")),
        num(cost, "{:.2f}"),
        str(len(result["gates"])),
        str(len(result["stops"])),
        str(len(result["off_card"])),
        "ок" if (final.get("check") or {}).get("ok") else "ошибки или нет проекта",
        comp.get("result") or "нет",
        result["outcome"],
    ]
    return "| " + " | ".join(cells) + " |"


def record(benchmark: Path, result: dict[str, Any]) -> None:
    """Добавляет строку в таблицу «Прогоны стенда»; нет раздела — создаёт его в конце файла."""
    text = benchmark.read_text(encoding="utf-8") if benchmark.is_file() else ""
    lines = text.splitlines()
    row = record_row(result)
    if RECORD_SECTION not in lines:
        lines += ["", RECORD_SECTION, "", RECORD_HEADER, _sep()]
    start = lines.index(RECORD_SECTION)
    end = start + 1
    while end < len(lines) and not (lines[end].startswith("## ") and end > start):
        end += 1
    last = max((i for i in range(start, end) if lines[i].startswith("|")), default=start)
    lines.insert(last + 1, row)
    benchmark.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _sep() -> str:
    return "|" + "---|" * (RECORD_HEADER.count("|") - 1)


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------


def product_state(repo: Path) -> tuple[str | None, bool | None]:
    """Коммит продукта и признак незафиксированных правок (`git`); нет `git` или репозитория — `None`."""

    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    changes = git("status", "--porcelain")
    return (commit.strip() if commit else None), (None if changes is None else bool(changes.strip()))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Стенд оценки: прогон агента на кейсе без человека")
    p.add_argument("case", help="номер кейса: папка в evals/cases/")
    p.add_argument("--mode", choices=["автопилот", "пилот"], default="автопилот")
    p.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"модель основного агента (по умолчанию {DEFAULT_MODEL})"
    )
    p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="предел вызовов claude -p")
    p.add_argument("--budget-usd", type=float, help="предел накопительной стоимости, USD")
    p.add_argument(
        "--max-minutes", type=float, default=DEFAULT_MAX_MINUTES, help="предел времени по часам, мин"
    )
    p.add_argument(
        "--call-timeout-min", type=float, default=DEFAULT_CALL_TIMEOUT_MIN, help="тайм-аут одного вызова, мин"
    )
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help="--max-turns одного вызова")
    p.add_argument("--dry-run", action="store_true", help="подменённый claude: без сети и без модели")
    p.add_argument("--record", action="store_true", help="добавить строку в docs/benchmark.md")
    p.add_argument("--out", type=Path, default=RUNS, help="где хранить итоги (по умолчанию evals/runs)")
    p.add_argument("--keep-copy", action="store_true", help="не удалять изолированную копию после прогона")
    p.add_argument(
        "--no-sync", action="store_true", help=argparse.SUPPRESS
    )  # проверки стенда: без uv sync в копии
    return p


def main(argv: list[str] | None = None, *, invoker: Invoker | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if args.record and args.dry_run:
        print(
            "--record не сочетается с --dry-run: строка о подменённом прогоне исказила бы таблицу",
            file=sys.stderr,
        )
        return EXIT_USAGE
    exe = None
    if not args.dry_run and invoker is None:
        exe = shutil.which("claude")
        if not exe:
            print(
                "claude не найден в PATH: прогон вызывает `claude -p`. Запустите в профиле, где Claude Code "
                "установлен и выполнен вход; цикл без claude проверяет --dry-run.",
                file=sys.stderr,
            )
            return EXIT_USAGE
    try:
        card = load_case(args.case)
    except StandError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE

    stamp = now().strftime("%Y%m%d-%H%M%S")
    out = args.out / args.case / stamp
    work = Path(tempfile.mkdtemp(prefix="strateg-eval-"))
    copy = work / "copy"
    try:
        build_copy(ROOT, copy)
        found = find_leaks(copy, leak_phrases(args.case, card), ROOT)
        if found:
            print("в копии нашлись строки кейса или ссылки на репозиторий: прогон отменён", file=sys.stderr)
            for line in found[:20]:
                print("  " + line, file=sys.stderr)
            return EXIT_USAGE
        settings = work / "run-settings.json"
        write_run_settings(settings, ROOT, copy)
        cli = make_cli(copy, sync=not args.no_sync)
        if args.dry_run:
            import dry_run

            invoker = dry_run.FakeClaude(cli, args.mode)
        elif invoker is None:
            assert exe is not None
            invoker = claude_process(exe)
        limits = Limits(
            args.max_calls, args.budget_usd, args.max_minutes, args.call_timeout_min, args.max_turns
        )
        bench = Bench(
            args.case, card, mode=args.mode, cli=cli, copy=copy, invoker=invoker, out=out, limits=limits,
            settings=settings, model=args.model, dry_run=args.dry_run,
        )  # fmt: skip
        bench.state.leak_check = {"phrases": leak_phrases(args.case, card), "found": []}
        bench.state.config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        bench.state.product_commit, bench.state.product_dirty = product_state(ROOT)
        if args.dry_run:
            bench.state.claude_version = "dry-run"
        elif exe is not None:
            bench.state.claude_version = claude_version(exe, clean_env(copy=copy), copy)
        code = bench.run(str(card["first_message"]).strip())
        result = bench.state.as_dict()
        if args.record:
            record(BENCHMARK, result)
        print(
            f"Итог: {result['outcome']}; вызовов {result['calls']}; "
            f"стоимость {result['total_cost_usd']}; итоги: {out}"
        )
        return code
    except StandError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        if args.keep_copy:
            print(f"копия оставлена: {copy}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
