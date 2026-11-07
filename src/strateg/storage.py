"""Запись файлов проекта, общая для CLI и хуков: блокировка папки проекта, уникальные временные файлы.

Журнал и `state.yaml` пишут и команды `strateg`, и хуки Claude Code (отдельные процессы, часто
параллельные). Без общей блокировки хеш журнала в `state.yaml` расходился бы с самим журналом (ложное
«изменено вне агента»), а событие могло потеряться. Правила:

- `project_lock` — межпроцессная блокировка папки проекта (файл `.tmp/lock`; Windows — `msvcrt`, POSIX —
  `fcntl`). Её отпускает система, если процесс упал, поэтому «забытой» блокировки не бывает. Внутри одного
  процесса блокировка вложенная. Ожидание ограничено: по таймауту — `LockTimeout` с понятным текстом;
- `atomic_write_bytes` — запись через временный файл с уникальным именем и `os.replace`;
- `append_journal` — «дописать строку журнала и обновить хеш журнала в `state.yaml`» одним действием под
  блокировкой.

Модуль лёгкий (без `yaml` и `pydantic`): его подключает хук прав, которому важна скорость старта.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import random
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

LOCK_REL = ".tmp/lock"
LOCK_TIMEOUT = 10.0  # CLI; хук берёт меньше: его собственный таймаут в настройках — 15 с
HOOK_LOCK_TIMEOUT = 5.0
LOCK_TIMEOUT_ENV = "STRATEG_LOCK_TIMEOUT"  # секунды; для тестов и для разбора зависшей блокировки
STATE_FILE, JOURNAL_FILE = "state.yaml", "journal.jsonl"
_JOURNAL_HASH = re.compile(r"(?m)^(  journal\.jsonl: )([0-9a-f]{64})$")
_REPLACE_TRIES = 40


def lock_timeout(default: float = LOCK_TIMEOUT) -> float:
    try:
        return float(os.environ[LOCK_TIMEOUT_ENV])
    except (KeyError, ValueError):
        return default


class LockTimeout(Exception):
    """Папку проекта не удалось занять за отведённое время."""

    def __init__(self, root: Path, seconds: float) -> None:
        super().__init__(
            f"папка проекта «{root.name}» занята другой командой или хуком больше {seconds:g} с "
            f"(блокировка {LOCK_REL}). Повтори через несколько секунд; если не отпускает, "
            "проверь, что не завис другой процесс `strateg`"
        )
        self.root = root
        self.seconds = seconds


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Блокировка --------------------------------------------------------------------------------------------


class _Entry:
    def __init__(self) -> None:
        self.guard = threading.RLock()  # потоки одного процесса
        self.depth = 0
        self.fd: int | None = None


_ENTRIES: dict[str, _Entry] = {}
_ENTRIES_GUARD = threading.Lock()


def _entry(root: Path) -> _Entry:
    key = os.path.normcase(os.path.abspath(root))
    with _ENTRIES_GUARD:
        return _ENTRIES.setdefault(key, _Entry())


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire_file(root: Path, deadline: float, timeout: float) -> int:
    path = root / LOCK_REL
    path.parent.mkdir(exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\n")  # блокируется первый байт: файл не должен быть пустым
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(root, timeout)
            time.sleep(random.uniform(0.01, 0.04))
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def project_lock(root: Path, timeout: float | None = None) -> Iterator[None]:
    """Занимает папку проекта на время блока. Вложенный вызов в том же процессе блокировку не повторяет."""
    timeout = lock_timeout() if timeout is None else timeout
    if not root.is_dir():  # проект создаётся или его нет: защищать нечего, и папку блокировка не создаёт
        yield
        return
    entry = _entry(root)
    deadline = time.monotonic() + timeout
    if not entry.guard.acquire(timeout=timeout):
        raise LockTimeout(root, timeout)
    try:
        if entry.depth == 0:
            entry.fd = _acquire_file(root, deadline, timeout)
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0 and entry.fd is not None:
                fd, entry.fd = entry.fd, None
                _unlock(fd)
    finally:
        entry.guard.release()


# --- Запись ------------------------------------------------------------------------------------------------


def temp_name(path: Path) -> Path:
    """Имя временного файла рядом с `path`: уникальное для процесса и вызова."""
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def replace_file(tmp: Path, path: Path) -> None:
    """`os.replace` с повторами: на Windows замену на миг задерживает читатель, открывший файл."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.025)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = temp_name(path)
    try:
        tmp.write_bytes(data)
        replace_file(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_bytes(path: Path, data: bytes) -> None:
    with path.open("ab") as f:
        f.write(data)


# --- Журнал и его хеш в state.yaml -------------------------------------------------------------------------


def recorded_journal_hash(state_text: str) -> str | None:
    m = _JOURNAL_HASH.search(state_text)
    return m.group(2) if m else None


def with_journal_hash(state_bytes: bytes, new_hash: str) -> bytes | None:
    """`state.yaml` с новым хешем журнала; None — в файле нет ровно одной строки с хешем журнала."""
    text = state_bytes.decode("utf-8-sig")
    new_text, n = _JOURNAL_HASH.subn(lambda m: m.group(1) + new_hash, text)
    if n != 1:
        return None
    return new_text.replace("\r\n", "\n").encode("utf-8")


def append_journal(root: Path, build: Callable[[bytes], bytes | None], timeout: float | None = None) -> bool:
    """Дописывает строку в `journal.jsonl` и, если журнал не правили руками, хеш журнала в `state.yaml`.

    `build` получает байты `state.yaml` и возвращает строку журнала (без перевода строки) либо None
    («писать нечего»). Всё под блокировкой папки проекта. Хеши других файлов не трогаются: правку человека
    принимает только команда CLI, записав «изменено вне агента». Если журнал сейчас длиннее записанного хеша
    (идущая команда CLI уже дописала свои события, а `state.yaml` ещё не сохранила), хеш не трогается:
    команда пересчитает его при сохранении. Возвращает False, если проект не удалось прочитать.
    """
    state_path, journal_path = root / STATE_FILE, root / JOURNAL_FILE
    with project_lock(root, timeout):
        try:
            state_bytes = state_path.read_bytes()
            journal_before = journal_path.read_bytes()
        except OSError:
            return False
        line = build(state_bytes)
        if line is None:
            return False
        line += b"\n"
        append_bytes(journal_path, line)
        try:
            recorded = recorded_journal_hash(state_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return True
        if recorded == sha256(journal_before):
            new_state = with_journal_hash(state_bytes, sha256(journal_before + line))
            if new_state is not None:
                atomic_write_bytes(state_path, new_state)
    return True
