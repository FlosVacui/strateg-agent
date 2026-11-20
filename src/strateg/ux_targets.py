"""Снимки экрана интерфейсов: список адресов разбора, сверка домена после перехода, команда `ux` (ADR-0013).

Человек даёт адреса сам: `strateg ux add <адрес>` (в Claude Code команда стоит в правиле `ask`) пишет их в
`projects/<id>/ux-targets.yaml`; файл правит только CLI. Исследователь переходит (`browser_navigate`) только
по адресу из списка целиком. После перехода хук `PostToolUse` сверяет итоговый адрес из ответа Playwright с
доменом запрошенного: чужой домен — снимок экрана закрыт до нового перехода по списку. Состояние перехода —
`.tmp/ux-nav.json` активного проекта; пишут его хуки.

Модуль лёгкий на импорте: его читает хук прав на каждый вызов браузера. Тяжёлые модули (`project`,
`commands`) подключаются внутри команд.

Команды: `ux add <адрес>`, `ux list`, `ux import <файл-снимка>` (снимок из временной папки `--output-dir`
сервера кладётся в `inputs/` проекта; дальше обычные `add` и `add --confirm`).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

TARGETS_FILE = "ux-targets.yaml"
NAV_DIR, NAV_NAME = ".tmp", "ux-nav.json"
SHOTS_NAME = "strateg-shots"  # папка снимков внутри временной: так же написан `--output-dir` в `.mcp.json`
SHOT_SUFFIXES = (".png", ".jpg", ".jpeg")
MAX_SHOT_BYTES = 20 * 1024 * 1024
MIN_NODE_MAJOR = 22
HOST_OK = re.compile(r"^[\w.-]+$")
IPV6_OK = re.compile(r"^[0-9a-f:.]+$")
PAGE_URL = re.compile(r"^[ \t]*-[ \t]*Page URL:[ \t]*(\S+)[ \t]*\r?$", re.MULTILINE)

PENDING, CONFIRMED, FOREIGN = "pending", "confirmed", "foreign"


# --- Адреса ------------------------------------------------------------------------------------------------


def normalize(url: str) -> str:
    """Адрес в одном виде: схема и хост строчными, порт по умолчанию и фрагмент убраны, путь не пуст.

    ValueError — адрес не http и не https, без хоста, с логином, пробелом или обратной косой чертой
    (браузер читает её как прямую, а разбор в Python — нет: расхождение дало бы обход списка).
    """
    text = url.strip() if isinstance(url, str) else ""
    if not text or any(c.isspace() or c == "\\" or ord(c) < 32 for c in text):
        raise ValueError("адрес пуст или содержит пробел, управляющий знак или обратную косую черту")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("нужен адрес http или https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("логин и пароль в адресе не допускаются")
    host = (parts.hostname or "").rstrip(".")
    if host and ":" not in host:
        try:
            host = host.encode("idna").decode("ascii")  # как браузер: кириллица — в punycode
        except UnicodeError:
            raise ValueError("имя хоста не кодируется (IDNA)") from None
    ok = IPV6_OK.match(host) if ":" in host else HOST_OK.match(host)
    if not host or not ok:
        raise ValueError("в адресе нет понятного имени хоста")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("порт в адресе не число") from None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (80 if scheme == "http" else 443):
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def site_of(url: str | None) -> str | None:
    """Домен адреса для сверки: хост строчными, без точки в конце и без `www.`; None — хоста нет."""
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".")
    if ":" not in host:
        try:
            host = host.encode("idna").decode("ascii")  # браузер называет итоговый адрес в punycode
        except UnicodeError:
            pass
    return host[4:] if host.startswith("www.") else host


def same_site(requested: str, final: str | None) -> bool:
    a = site_of(requested)
    return a is not None and a == site_of(final)


def _strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)


def final_url(response: Any) -> str | None:
    """Итоговый адрес из ответа `browser_navigate`: строка «- Page URL: <адрес>» блока «### Page».

    Формат сверен пробой сервера (`scripts/probe_playwright_mcp.py`). Берётся первая такая строка: заголовок
    страницы идёт ниже и в одну строку. Адреса в ответе нет — None.
    """
    texts: list[str] = []
    _strings(response, texts)
    found = PAGE_URL.search("\n".join(texts))
    return found.group(1) if found else None


# --- Файл списка -------------------------------------------------------------------------------------------


def read_items(root: Path) -> list[dict[str, Any]]:
    """Записи списка проекта; нет файла — пусто. Битый файл — исключение: хук отказывает, CLI сообщает."""
    import yaml

    path = root / TARGETS_FILE
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_bytes().decode("utf-8-sig"))
    items = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{TARGETS_FILE}: нет списка targets")
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("url"), str)]


def read_urls(root: Path) -> tuple[str, ...]:
    """Нормализованные адреса списка для хука; любая ошибка чтения — пустой список (переход закрыт)."""
    try:
        return tuple(normalize(i["url"]) for i in read_items(root))
    except Exception:  # noqa: BLE001 - закрыто при любом сбое
        return ()


def write_items(root: Path, items: list[dict[str, Any]]) -> None:
    import yaml

    from strateg import storage

    text = yaml.safe_dump(
        {"schema_version": 1, "targets": items}, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    storage.atomic_write_bytes(root / TARGETS_FILE, text.encode("utf-8"))


# --- Состояние перехода ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NavState:
    status: str  # PENDING, CONFIRMED или FOREIGN
    requested: str
    final: str | None = None


def read_nav(root: Path) -> NavState | None:
    """Состояние последнего перехода; нет файла или он не читается — None (снимок закрыт)."""
    try:
        data = json.loads((root / NAV_DIR / NAV_NAME).read_bytes().decode("utf-8"))
        status, requested = data["status"], data["requested"]
        final = data.get("final")
        if status not in (PENDING, CONFIRMED, FOREIGN) or not isinstance(requested, str):
            return None
        return NavState(status, requested, final if isinstance(final, str) else None)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_nav(root: Path, state: NavState) -> None:
    from strateg import storage

    folder = root / NAV_DIR
    folder.mkdir(parents=True, exist_ok=True)
    body = {"status": state.status, "requested": state.requested, "final": state.final}
    storage.atomic_write_bytes(folder / NAV_NAME, json.dumps(body, ensure_ascii=False).encode("utf-8"))


def verify(requested: str, response: Any, known: str | None = None) -> NavState:
    """Итог перехода по ответу инструмента: домен итогового адреса тот же — подтверждён; чужой — нет.

    Сервер без снимка страницы называет адрес («Page URL»), только если изменился заголовок вкладки; без
    блока «Page» вкладка осталась на прежнем адресе. Поэтому тогда сверяется последний известный итоговый
    адрес `known`; его нет — переход не подтверждён (`PENDING`).
    """
    final = final_url(response) or known
    if final is None:
        return NavState(PENDING, requested, None)
    return NavState(CONFIRMED if same_site(requested, final) else FOREIGN, requested, final)


def verify_shot(prior: NavState | None, response: Any) -> NavState | None:
    """Ответ снимка тоже может назвать адрес страницы: он сменился после загрузки — итог обновляется.

    None — сверять нечего (перехода не было или адреса в ответе нет).
    """
    final = final_url(response)
    if prior is None or final is None:
        return None
    return NavState(CONFIRMED if same_site(prior.requested, final) else FOREIGN, prior.requested, final)


# --- Команды -----------------------------------------------------------------------------------------------


def shots_dir(env: dict[str, str] | None = None) -> Path:
    """Папка снимков, как в `scripts/playwright_mcp.py`: `<временная папка ОС>/strateg-shots`."""
    if env is None:
        return Path(tempfile.gettempdir()) / SHOTS_NAME
    return Path(env.get("TEMP") or env.get("TMPDIR") or "/tmp") / SHOTS_NAME


def _open(args: argparse.Namespace) -> Any:
    from strateg import project as proj

    return proj.Project.load(proj.resolve_project(getattr(args, "project", None)))


def cmd_ux_add(args: argparse.Namespace) -> int:
    from strateg import project as proj
    from strateg.commands import emit
    from strateg.project import UsageError

    p = _open(args)
    try:
        url = normalize(args.url)
    except ValueError as exc:
        raise UsageError(f"адрес «{args.url}» не принят: {exc}") from None
    with proj.transaction(p):
        try:
            items = read_items(p.root)
        except Exception as exc:  # noqa: BLE001 - файл правил испорчен: не затираем
            raise proj.CliError(f"{TARGETS_FILE} не читается: {exc}") from exc
        if any(normalize(i["url"]) == url for i in items):
            return emit(args, {"url": url, "changed": False}, [f"{url} уже в списке разбора."])
        items.append({"url": url, "added": proj.now().isoformat()})
        write_items(p.root, items)
        p.add_event(
            p.event(
                kind="действие",
                name="strateg ux add",
                idempotency_hash=proj.key_hash("ux add", url),
                status="выполнено",
                duration_s=0,
                message=f"адрес для снимков экрана: {url}",
            )
        )
        p.save_state()
    return emit(
        args,
        {"url": url, "changed": True, "count": len(items)},
        [f"{url} внесён в список разбора ({len(items)}): исследователь снимает экраны только по нему."],
    )


def cmd_ux_list(args: argparse.Namespace) -> int:
    from strateg.commands import emit
    from strateg.project import CliError

    p = _open(args)
    try:
        urls = [normalize(i["url"]) for i in read_items(p.root)]
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"{TARGETS_FILE} не читается: {exc}") from exc
    lines = urls or ["Список разбора пуст: адрес вносит человек командой `strateg ux add <адрес>`."]
    return emit(args, {"targets": urls}, lines)


def cmd_ux_import(args: argparse.Namespace) -> int:
    """Снимок из временной папки сервера — в `inputs/` проекта: туда `strateg add` берёт материалы."""
    from strateg import project as proj
    from strateg.commands import emit
    from strateg.project import CliError

    p = _open(args)
    given = Path(args.file)
    source = given.resolve() if given.is_absolute() else (Path.cwd() / given).resolve()
    folder = shots_dir().resolve()
    if folder not in source.parents:
        raise CliError(f"снимок должен лежать в {folder}: туда его кладёт сервер браузера")
    if source.suffix.lower() not in SHOT_SUFFIXES or not source.is_file():
        raise CliError("нужен файл снимка .png или .jpg из этой папки")
    if source.stat().st_size > MAX_SHOT_BYTES:
        raise CliError("снимок больше 20 МБ")
    name = f"ux-{source.name}"
    target = p.root / "inputs" / name
    body = source.read_bytes()
    with proj.transaction(p):
        target.parent.mkdir(exist_ok=True)
        if target.exists() and target.read_bytes() != body:
            raise CliError(f"inputs/{name} уже есть с другим содержимым")
        again = target.exists()
        if not again:
            target.write_bytes(body)  # те же байты, что сверены выше: файл не подменить
        if again:  # повтор: тот же файл уже на месте, второго события и нового номера нет
            return emit(
                args, {"path": f"inputs/{name}", "changed": False}, [f"Снимок уже лежит в inputs/{name}."]
            )
        p.add_event(
            p.event(
                kind="действие",
                name="strateg ux import",
                idempotency_hash=proj.key_hash("ux import", name, proj.sha256_bytes(body)),
                status="выполнено",
                duration_s=0,
                message=f"снимок экрана из временной папки сервера положен в inputs/{name}",
            )
        )
        p.save_state()
    return emit(
        args,
        {"path": f"inputs/{name}", "changed": True},
        [
            f"Снимок положен в inputs/{name}.",
            f"Дальше: `uv run strateg add inputs/{name}`, затем человек подтверждает "
            "`uv run strateg add --confirm И-n` (на экране нет лиц и контактов).",
        ],
    )


def register(sub: Any, common: argparse.ArgumentParser, parser_class: type) -> None:
    """Подкоманды `ux add|list|import`: вызывается из `cli.build_parser`."""
    from strateg import commands

    ux = sub.add_parser("ux", parents=[common], help="адреса и снимки экрана интерфейсов (Playwright)")
    ux_sub = ux.add_subparsers(dest="ux_command", required=True, parser_class=parser_class)
    p = ux_sub.add_parser(
        "add", parents=[common], help="внести адрес в список разбора (команду подтверждает человек)"
    )
    p.set_defaults(func=commands.run, handler=cmd_ux_add)
    p.add_argument("url", help="адрес http или https")
    p = ux_sub.add_parser("list", parents=[common], help="показать список разбора")
    p.set_defaults(func=commands.run, handler=cmd_ux_list)
    p = ux_sub.add_parser("import", parents=[common], help="положить снимок экрана в inputs/ проекта")
    p.set_defaults(func=commands.run, handler=cmd_ux_import)
    p.add_argument("file", help="файл снимка из временной папки сервера браузера")


# --- doctor ------------------------------------------------------------------------------------------------


def node_line(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[str, str]:
    """Уровень и пояснение для `doctor`: Node нужен только для снимков экрана (предупреждение, не ошибка)."""
    exe = which("node")
    if not exe:
        return "WARN", "не найден: снимки экрана интерфейсов недоступны, разбор — по снимкам человека"
    try:
        out = run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "WARN", f"{exe}: версию определить не удалось; снимки экрана могут не работать"
    found = re.match(r"v?(\d+)", out)
    if not found:
        return "WARN", f"{exe}: версию определить не удалось; снимки экрана могут не работать"
    if int(found.group(1)) < MIN_NODE_MAJOR:
        return "WARN", f"{out}: ниже {MIN_NODE_MAJOR}, снимки экрана могут не работать; обновите Node"
    if not which("npx"):
        return "WARN", f"{out}, но нет npx: сервер снимков экрана не запустится"
    return "OK", out
