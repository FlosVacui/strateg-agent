"""Дочерний процесс расчёта: исполняет скрипт `outputs/calc/<имя>.py` под охраной аудита (`strateg calc run`).

Запускается отдельным интерпретатором (`python -s -B calcrun.py`), на входе (stdin) — описание запуска
в JSON, на выходе (stdout) — один JSON: напечатанное скриптом, прочитанные источники, нарушения.
Это не песочница ОС, а охрана от ошибок и подсказок из чужого текста: хук аудита Python закрывает
сеть, запуск программ, `ctypes`, запись файлов и чтение вне `sources/` и текстов `inputs/` проекта
(только перечисленные в описании снимки) и библиотек Python. Намеренно злой код хук обойти может
(например, через `__closure__` самого хука); этого уровня защиты здесь нет.

Скрипт получает модуль `strateg_calc`: `table("И-n")`, `text("И-n")`, `number(строка)`, `emit(словарь)`.
Только стандартная библиотека и `strateg.tables`; ничего из проекта, кроме перечисленных снимков.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import sysconfig
import traceback
import types

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_OUT = 1_000_000
MAX_ERR = 20_000


class _Limited(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit, self.parts, self.size = limit, [], 0

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self.size += len(s)
        if self.size > self.limit:
            raise PermissionError(f"скрипт напечатал больше {self.limit} знаков: результат — небольшой JSON")
        self.parts.append(s)
        return len(s)

    def getvalue(self) -> str:
        return "".join(self.parts)


def _norm(path: object) -> str:
    return os.path.normcase(os.path.realpath(os.fsdecode(path)))  # type: ignore[arg-type]


def main() -> int:
    spec = json.loads(sys.stdin.read())
    if sys.path:
        sys.path.pop(0)  # папка этого файла: другие модули пакета расчёту не нужны
    if PKG_PARENT not in sys.path:
        sys.path.insert(0, PKG_PARENT)
    from strateg import tables as tb

    root = spec["root"]
    sources = spec["sources"]  # {"И-1": {"path": "inputs/i-1.txt", "sha256": "..."}}
    script_path = os.path.join(root, spec["script"])
    with open(script_path, "rb") as fh:
        code = compile(fh.read(), script_path, "exec")

    allowed_files = {_norm(os.path.join(root, v["path"])) for v in sources.values()} | {_norm(script_path)}
    lib = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(os.__file__)}
    lib |= {
        v for k, v in sysconfig.get_paths().items() if k in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    lib |= {p for p in sys.path if p}
    lib_dirs = tuple(_norm(p) + os.sep for p in lib)
    null = {os.path.normcase(os.devnull), "nul"}
    violations: list[str] = []
    used: dict[str, str] = {}

    def deny(what: str) -> None:
        msg = f"закрыто: {what}"
        violations.append(msg)
        raise PermissionError(msg)

    def readable(path: object) -> bool:
        if isinstance(path, int):
            return True
        try:
            p = _norm(path)
        except (TypeError, ValueError, OSError):
            return False
        return p in allowed_files or p.startswith(lib_dirs) or os.path.normcase(str(path)) in null

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    deny_prefix = (
        "socket.",
        "ctypes.",
        "subprocess.",
        "shutil.",
        "winreg.",
        "webbrowser.",
        "smtplib.",
        "ftplib.",
    )
    deny_events = {
        "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty", "os.startfile",
        "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.link", "os.symlink",
        "os.truncate", "os.utime", "os.mkfifo", "os.mknod", "os.putenv", "os.unsetenv", "os.kill",
        "os.killpg", "os.setxattr", "os.removexattr", "tempfile.mkstemp", "tempfile.mkdtemp",
        "urllib.Request", "http.client.connect", "pty.spawn", "mmap.__new__",
    }  # fmt: skip
    deny_imports = {
        "ctypes",
        "_ctypes",
        "subprocess",
        "_winapi",
        "multiprocessing",
        "winreg",
        "_socket",
        "socket",
    }

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            if path is None:
                return
            writing = bool(flags & write_flags) if isinstance(flags, int) else False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if writing and os.path.normcase(str(path)) not in null and not isinstance(path, int):
                deny(f"запись в файл {path}: расчёт ничего не пишет, результат — JSON в stdout")
            if not readable(path):
                deny(f"чтение файла {path}: расчёт читает только источники проекта по номеру И-n")
        elif event in ("os.listdir", "os.scandir"):
            path = args[0]
            if path is None or not readable(os.path.join(os.fsdecode(path), "x")):
                deny(f"просмотр папки {path}")
        elif event == "import":
            if args[0].split(".")[0] in deny_imports:
                deny(f"модуль {args[0]}: сети и запуска программ у расчёта нет")
        elif event in deny_events or event.startswith(deny_prefix):
            deny(f"{event}: сети, запуска программ и записи у расчёта нет")

    # --- помощник для скрипта ---
    class Tab:
        def __init__(self, table: tb.Table) -> None:
            self._t = table
            self.sheet = table.sheet
            self.columns = [c.name for c in table.columns]
            self.rows = table.records()

        def column(self, name: str) -> list[str | None]:
            return list(self._t.column(name).values)

        def numbers(self, name: str, missing: str = "skip") -> list[float | None]:
            """Числа столбца. Пропуски по умолчанию пропускаются; `missing="none"` оставляет None на месте."""
            out: list[float | None] = []
            for v in self._t.column(name).values:
                parsed = tb.parse_number(v) if v is not None else None
                if parsed is not None:
                    out.append(parsed[0])
                elif missing == "none":
                    out.append(None)
            return out

        def __len__(self) -> int:
            return len(self.rows)

    def text(source: str) -> str:
        if source not in sources:
            raise KeyError(
                f"источник {source} недоступен расчёту (нет в проекте, нет текста или не подтверждён)"
            )
        info = sources[source]
        with open(os.path.join(root, info["path"]), "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError(f"текст {source} изменился во время расчёта")
        used[source] = info["sha256"]
        return data.decode("utf-8")

    def table(source: str, sheet: str | None = None) -> Tab:
        found = tb.tables_of(text(source))
        if sheet is not None:
            found = [t for t in found if t.sheet == sheet]
        if not found:
            raise KeyError(f"в {source} нет таблицы" + (f" на листе «{sheet}»" if sheet else ""))
        return Tab(found[0])

    def number(value: str) -> float:
        parsed = tb.parse_number(value)
        if parsed is None:
            raise ValueError(f"«{value}» не число")
        return parsed[0]

    result: list[str] = []

    def emit(obj: object) -> None:
        result.append(json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False))

    helper = types.ModuleType("strateg_calc")
    helper.table, helper.text, helper.number, helper.emit = table, text, number, emit  # type: ignore[attr-defined]
    sys.modules["strateg_calc"] = helper

    real_out, real_err = sys.stdout, sys.stderr
    out, err = _Limited(MAX_OUT), _Limited(MAX_ERR)
    sys.stdout, sys.stderr = out, err
    status = 0
    try:
        sys.addaudithook(hook)
        scope = {"__name__": "__main__", "__file__": script_path, "__builtins__": __builtins__}
        exec(code, scope)  # noqa: S102 - расчёт агента под охраной хука
    except SystemExit as exc:
        status = 0 if exc.code in (None, 0) else 1
    except BaseException:  # noqa: BLE001
        status = 1
        err.parts.append(traceback.format_exc()[-MAX_ERR:])
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    stdout = "\n".join(result) if result else out.getvalue()
    real_out.write(
        json.dumps(
            {"status": status, "stdout": stdout, "stderr": err.getvalue()[-MAX_ERR:], "used": used,
             "violations": violations},
            ensure_ascii=False,
        )
    )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
