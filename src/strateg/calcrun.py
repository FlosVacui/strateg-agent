"""Дочерний процесс расчёта: исполняет `outputs/calc/<имя>.py` под двумя слоями охраны (`strateg calc run`).

Слой 1 — разбор скрипта до запуска (`check_script`, AST): импорт только из `ALLOWED_IMPORTS`; закрыты `open`,
`eval`, `exec`, `compile`, `getattr`, имена с `__` и скрытые атрибуты (`_x`, `sys`, `gi_frame`…).
Слой 2 — хук аудита Python в дочернем процессе (`python -s -B calcrun.py`): сеть, запуск программ, `ctypes`,
запись файлов, чтение вне `sources/` и текстов `inputs/` проекта. Модули `_winapi`, `sqlite3`, `msvcrt` до
запуска убраны из `sys.modules`, дескрипторы 1 и 2 направлены в `nul`, а настоящий вывод процесс держит под
другим номером. На входе (stdin) — описание запуска в JSON, на выходе — один JSON: напечатанное скриптом,
прочитанные источники, нарушения.

Это охрана от ошибок и подсказок из чужого текста, а не песочница ОС. Остаётся: память (предела нет, в Windows
без Job Object нечем), `gc.get_objects` и замыкания хука при обходе разбора через строки, наблюдение за
файловой системой (`os.stat`). Намеренно злой код такой защиты обойдёт.

Скрипт получает модуль `strateg_calc`: `table("И-n")`, `text("И-n")`, `number(строка)`, `emit(словарь)`;
у таблицы — `columns`, `rows`, `column(имя)`, `numbers(имя)`, `missing(имя)`, `len()`.
"""

from __future__ import annotations

import ast
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


ALLOWED_IMPORTS = frozenset(
    {"math", "statistics", "decimal", "fractions", "datetime", "json", "itertools", "collections", "re"}
)
HELPER = "strateg_calc"
_BANNED_NAMES = frozenset(
    {"eval", "exec", "compile", "open", "getattr", "setattr", "delattr", "globals", "locals", "vars", "dir",
     "breakpoint", "input", "help", "memoryview", "exit", "quit"}
)  # fmt: skip
# атрибуты, через которые выходят к системе, кадрам стека и загруженным модулям
_BANNED_ATTRS = frozenset(
    {"sys", "os", "nt", "posix", "io", "builtins", "codecs", "importlib", "subprocess", "gc", "modules",
     "environ", "system", "popen", "mro"}
)  # fmt: skip
_BANNED_ATTR_PREFIXES = ("gi_", "cr_", "ag_", "f_", "tb_", "co_", "func_")
_MODULES_OFF = ("_winapi", "_overlapped", "msvcrt", "sqlite3", "_sqlite3", "winreg", "_winreg")


class ScriptSyntaxError(Exception):
    """Скрипт не разбирается: сообщение с номером строки."""


def _identifiers(node: ast.AST) -> list[tuple[str, str]]:
    """(вид, имя) всех имён, которые задаёт или называет узел."""
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Name):
        out.append(("имя", node.id))
    elif isinstance(node, ast.Attribute):
        out.append(("атрибут", node.attr))
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.append(("имя", node.name))
    elif isinstance(node, ast.arg):
        out.append(("имя", node.arg))
    elif isinstance(node, ast.keyword) and node.arg:
        out.append(("имя", node.arg))
    elif isinstance(node, ast.alias) and node.asname:
        out.append(("имя", node.asname))
    elif isinstance(node, ast.ExceptHandler) and node.name:
        out.append(("имя", node.name))
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        out += [("имя", n) for n in node.names]
    return out


def _import_problem(name: str) -> str | None:
    parts = name.split(".")
    if any(p.startswith("_") for p in parts) and name != "__future__":
        return f"модуль {name}: имена с «_» закрыты"
    if name == "__future__" or parts[0] == HELPER and len(parts) == 1:
        return None
    if parts[0] not in ALLOWED_IMPORTS:
        return f"модуль {name}: разрешены только {', '.join(sorted(ALLOWED_IMPORTS))} и {HELPER}"
    if parts[-1] in _BANNED_ATTRS:
        return f"модуль {name}: закрыт"
    return None


def check_script(source: bytes | str) -> list[str]:
    """Нарушения правил скрипта (пусто — можно запускать); `ScriptSyntaxError`, если он не разбирается."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptSyntaxError(f"синтаксическая ошибка в строке {exc.lineno} скрипта: {exc.msg}") from None
    except ValueError as exc:  # нулевые байты и т. п.
        raise ScriptSyntaxError(f"скрипт не разбирается: {exc}") from None
    problems: list[str] = []

    def bad(node: ast.AST, what: str) -> None:
        problems.append(f"строка {getattr(node, 'lineno', '?')}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (why := _import_problem(alias.name)) is not None:
                    bad(node, why)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                bad(node, "относительный импорт закрыт")
            elif (why := _import_problem(node.module)) is not None:
                bad(node, why)
            else:
                for alias in node.names:
                    if alias.name.startswith("_") or alias.name in _BANNED_ATTRS:
                        bad(node, f"имя {alias.name} из {node.module}: закрыто")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "{" in node.value and "__" in node.value:
                bad(node, "строка формата с «__»: так добираются до скрытых атрибутов")
        for kind, name in _identifiers(node):
            if kind == "имя" and name in _BANNED_NAMES:
                bad(node, f"{name}: закрыто (чтение файлов и запуск кода из строк расчёту не нужны)")
            elif name.startswith("__") and not (name == "__name__" and isinstance(node, ast.Name)):
                bad(node, f"{kind} {name}: имена с «__» закрыты")
            elif kind == "атрибут" and (
                name.startswith("_") or name in _BANNED_ATTRS or name.startswith(_BANNED_ATTR_PREFIXES)
            ):
                bad(node, f"атрибут {name}: закрыт")
    return problems


def _redirect_fds() -> io.TextIOBase:
    """Дескрипторы 1 и 2 — в `nul`; настоящий вывод процесса держит возвращённый файл под другим номером."""
    keep = os.dup(1)
    nul = os.open(os.devnull, os.O_WRONLY)
    os.dup2(nul, 1)
    os.dup2(nul, 2)
    os.close(nul)
    return os.fdopen(keep, "w", encoding="utf-8", newline="")


def main() -> int:
    spec = json.loads(sys.stdin.read())
    reply = _redirect_fds()

    def finish(obj: dict) -> int:
        reply.write(json.dumps(obj, ensure_ascii=False))
        reply.flush()
        return 0

    if sys.path:
        sys.path.pop(0)  # папка этого файла: другие модули пакета расчёту не нужны
    if PKG_PARENT not in sys.path:
        sys.path.insert(0, PKG_PARENT)
    from strateg import tables as tb

    root = spec["root"]
    sources = spec["sources"]  # {"И-1": {"path": "inputs/i-1.txt", "sha256": "..."}}
    script_path = os.path.join(root, spec["script"])
    with open(script_path, "rb") as fh:
        source = fh.read()
    if spec.get("guard", True):  # разбор скрипта; снимается только в тестах хука, скрипту это недоступно
        try:
            problems = check_script(source)
        except ScriptSyntaxError as exc:
            problems = [str(exc)]
        if problems:
            return finish({"status": 1, "stdout": "", "stderr": "", "used": {},
                           "violations": [f"закрыто разбором: {p}" for p in problems]})  # fmt: skip
    code = compile(source, script_path, "exec")

    by_path = {_norm(os.path.join(root, v["path"])): sid for sid, v in sources.items()}
    allowed_files = set(by_path) | {_norm(script_path)}
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
        "sqlite3.",
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
        "sqlite3",
        "_sqlite3",
        "msvcrt",
        "_overlapped",
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
            sid = by_path.get(_norm(path)) if not isinstance(path, int) else None
            if sid is not None:  # источник считается входом, как бы его ни прочитали
                used[sid] = sources[sid]["sha256"]
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
            """Числа столбца. Пропуски («н/д», «-», пустая ячейка) по умолчанию пропускаются, число их — в
            `missing(имя)`; `missing="none"` оставляет None на месте. Текст, не число, — ошибка."""
            col = self._t.column(name)
            out: list[float | None] = []
            for v in col.values:
                if v is None:
                    if missing == "none":
                        out.append(None)
                    continue
                parsed = tb.parse_number(v)
                if parsed is None:
                    raise ValueError(f"в столбце «{name}» значение «{v}» не число")
                out.append(parsed[0])
            return out

        def missing(self, name: str) -> int:
            """Сколько в столбце пропусков: пустых ячеек и заглушек («н/д», «нет данных», «-»)."""
            return self._t.column(name).missing

        def __len__(self) -> int:
            return len(self.rows)

    def text(source: str) -> str:
        if source not in sources:
            raise KeyError(
                f"источник {source} недоступен расчёту: "
                + spec.get("refused", {}).get(source, "его нет в проекте")
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
        for name in _MODULES_OFF:  # `import` падает, а событие аудита не приходит: хуку тут нечего ловить
            sys.modules[name] = None  # type: ignore[assignment]
        sys.addaudithook(hook)
        scope = {"__name__": "__main__", "__file__": script_path, "__builtins__": __builtins__}
        exec(code, scope)  # noqa: S102 - расчёт агента под охраной разбора и хука
    except SystemExit as exc:
        status = 0 if exc.code in (None, 0) else 1
    except BaseException:  # noqa: BLE001
        status = 1
        err.parts.append(traceback.format_exc()[-MAX_ERR:])
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    stdout = chr(10).join(result) if result else out.getvalue()
    return finish(
        {"status": status, "stdout": stdout, "stderr": err.getvalue()[-MAX_ERR:], "used": used,
         "violations": violations}
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
