"""Каждая команда, меняющая проект, идёт через `transaction(проект)`: проект перечитывается под блокировкой.

`transaction(p.root)` держит блокировку, но состояние остаётся прочитанным при открытии команды: номер и
запись строятся по устаревшему снимку. Поведение одного процесса при этом не отличается, поэтому правку
ловит разбор исходников, а не прогон команды. Разбираются все модули `src/strateg/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "strateg"
MODULES = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
WRITES = {"add_event", "add_cost", "save_state", "save_sources", "save_claims"}
# Помимо `cmd_*`: функции, которые меняют проект, но не называются командой.
EXTRA = {"register", "confirm", "_pick", "_set_profile", "_resume"}
# Команды, которые пишут только производные файлы вне состояния проекта: `export` собирает HTML и PDF в
# `export/` (или в `--out`) и не трогает файлы состояния, выводов и журнал. Блокировка им не нужна.
DERIVED_ONLY = {"export.py:cmd_export"}


def parsed(path: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def is_transaction(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "attr", None) == "transaction" or getattr(node.func, "id", None) == "transaction"
    )


def mentions_root(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "root" for n in ast.walk(node))


def root_aliases(fn: ast.FunctionDef) -> set[str]:
    """Имена, которым в функции присвоено что-то от `.root` (`_root = p.root`), в том числе по цепочке."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None:
                uses_alias = any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(n.value))
                if mentions_root(n.value) or uses_alias:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id not in names:
                            names.add(t.id)
                            changed = True
    return names


def test_every_transaction_takes_the_project_not_its_folder_or_an_alias_of_it():
    found = 0
    for module in MODULES:
        for fn in parsed(module):
            aliases = root_aliases(fn)
            for call in (c for c in ast.walk(fn) if is_transaction(c)):
                found += 1
                arg = call.args[0]
                bad = mentions_root(arg) or (isinstance(arg, ast.Name) and arg.id in aliases)
                assert not bad, (
                    f"{module.name}:{call.lineno} ({fn.name}): `transaction({ast.unparse(arg)})` не "
                    "перечитывает проект под блокировкой; передайте сам проект"
                )
    assert found >= 25  # разбор не потерял вызовы


def test_alias_of_the_folder_is_caught_by_the_same_scan():
    fn = ast.parse(
        "def cmd_x(p):\n    _root = p.root\n    with proj.transaction(_root):\n        pass\n"
    ).body[0]
    (call,) = (c for c in ast.walk(fn) if is_transaction(c))
    assert isinstance(call.args[0], ast.Name) and call.args[0].id in root_aliases(fn)


def inside(blocks: list[tuple[int, int]], line: int) -> bool:
    return any(a <= line <= b for a, b in blocks)


def is_write(call: ast.Call) -> bool:
    func = call.func
    attr = getattr(func, "attr", None)
    if attr in WRITES or attr == "atomic_write_bytes":
        return True
    return attr == "write_text" and isinstance(func.value, ast.Name) and func.value.id == "proj"


def test_every_write_of_a_command_stands_inside_its_transaction_block():
    checked = []
    for module in MODULES:
        for fn in parsed(module):
            if not (fn.name.startswith("cmd_") or fn.name in EXTRA):
                continue
            if f"{module.name}:{fn.name}" in DERIVED_ONLY:
                continue
            blocks = [
                (w.body[0].lineno, w.end_lineno)
                for w in ast.walk(fn)
                if isinstance(w, ast.With) and any(is_transaction(i.context_expr) for i in w.items)
            ]
            writes = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and is_write(c)]
            if not writes:
                continue
            checked.append(f"{module.name}:{fn.name}")
            assert blocks, f"{module.name}: {fn.name} пишет проект вне `with transaction(проект)`"
            for call in writes:
                assert inside(blocks, call.lineno), (
                    f"{module.name}:{call.lineno} ({fn.name}): `{ast.unparse(call.func)}` стоит вне блока "
                    "`with transaction(проект)`: запись идёт по состоянию, прочитанному без блокировки"
                )
    assert len(checked) >= 15, checked


def test_write_and_pack_check_the_project_under_the_lock(cli, home, monkeypatch):
    """Пока соседняя команда пишет снимок и реестр, проверка не видит полузаписанного."""
    from helpers import put_frame
    from strateg import commands, pack, storage

    assert cli("new", "demo")[0] == 0
    seen = []
    for module in (commands, pack):
        real = module.check_project
        monkeypatch.setattr(
            module,
            "check_project",
            lambda root, real=real: seen.append(storage.lock_held(root)) or real(root),
        )
    put_frame(cli)
    pack.verify(home / "demo")
    assert len(seen) == 2 and all(seen)
