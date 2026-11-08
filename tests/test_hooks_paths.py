"""Приведение путей для хуков прав: Windows, macOS и Linux проверяются явными примерами на любой машине.

ОС задаётся полем `os_name`; файловую систему трогает только `realpath`, его подменяют.
"""

from __future__ import annotations

import dataclasses

import pytest

from strateg.hooks import paths
from strateg.hooks.paths import PathEnv, places

WIN_HOME = r"D:\Home\Иван Петров"
WIN_ROOT = WIN_HOME + r"\strateg agent"
NT = PathEnv(
    os_name="nt",
    root=WIN_ROOT,
    projects=WIN_ROOT + r"\projects",
    home=WIN_HOME,
    cwd=WIN_ROOT,
    env={"USERPROFILE": WIN_HOME, "HOME": WIN_HOME},
    claude_dir=WIN_HOME + r"\.claude",
)
LINUX = PathEnv(
    os_name="posix",
    root="/home/ivan/strateg agent",
    projects="/home/ivan/strateg agent/projects",
    home="/home/ivan",
    cwd="/home/ivan/strateg agent",
    env={"HOME": "/home/ivan"},
    claude_dir="/home/ivan/.claude",
)
MAC = PathEnv(
    os_name="posix",
    macos=True,
    root="/Users/ivan/Strateg Agent",
    projects="/Users/ivan/Strateg Agent/projects",
    home="/Users/ivan",
    cwd="/Users/ivan/Strateg Agent",
    env={"HOME": "/Users/ivan"},
    claude_dir="/Users/ivan/.claude",
)
ALL = pytest.mark.parametrize("penv", [NT, LINUX, MAC], ids=["windows", "linux", "macos"])


def one(raw: str, penv: PathEnv) -> paths.Place:
    found = places(raw, penv)
    assert found is not None, f"путь {raw!r} не привёлся"
    assert len(found) == 1  # realpath не задан
    return found[0]


def test_case_sensitivity_by_os():
    assert NT.case_insensitive and MAC.case_insensitive and not LINUX.case_insensitive


@ALL
def test_relative_paths_inside_project(penv):
    pl = one("projects/demo/sources/page.txt", penv)
    assert pl.in_root and pl.in_projects and pl.project_id == "demo"
    assert pl.project_rel == ("sources", "page.txt")
    assert not pl.sensitive and not pl.rules


@ALL
def test_dotdot_cannot_climb_out_of_sources(penv):
    pl = one("projects/demo/sources/../inputs/a.txt", penv)
    assert pl.project_rel == ("inputs", "a.txt")


def test_backslash_is_a_separator_on_every_os():
    # на Linux обратная черта — обычный знак имени, но проверка идёт как на Windows: так строже
    for penv in (NT, LINUX, MAC):
        assert one(r"projects\demo\sources\..\inputs\a.txt", penv).project_rel == ("inputs", "a.txt")


def test_case_is_folded_on_windows_and_macos_only():
    for penv in (NT, MAC):
        assert one("Projects/Demo/Sources/../Inputs/a.txt".replace("Demo", "demo"), penv).project_rel == (
            "inputs",
            "a.txt",
        )
        assert one("PROJECTS/demo/.TMP/PENDING/x", penv).pending
        assert one("EVALS/case.yaml", penv).evals
        assert one(".ENV", penv).env_file
        assert one("agents.MD", penv).rules
    # на Linux `Evals` и `.ENV` — другие имена
    assert not one("Evals/case.yaml", LINUX).evals
    assert not one(".ENV", LINUX).env_file
    assert one("evals/case.yaml", LINUX).evals
    assert one("AGENTS.md", LINUX).rules and not one("agents.md", LINUX).rules


@ALL
def test_env_files(penv):
    assert one(".env", penv).env_file
    assert one("projects/demo/.env", penv).env_file
    assert one("sub/.env.local", penv).env_file
    assert not one(".env.example", penv).env_file
    assert not one("environment.md", penv).env_file


@ALL
def test_pending_and_evals(penv):
    assert one("projects/demo/.tmp/pending/source.txt", penv).pending
    assert not one("projects/demo/.tmp/frame.yaml", penv).pending
    assert one("evals/cases/a.yaml", penv).evals
    assert not one("docs/evals-notes.md", penv).evals


@ALL
def test_rules_files(penv):
    assert one("AGENTS.md", penv).rules
    assert one("CLAUDE.md", penv).rules
    assert one(".claude/settings.json", penv).rules
    assert one(".claude/hooks/pre_tool_use.py", penv).rules
    assert not one("README.md", penv).rules
    assert not one("knowledge/roles/researcher.md", penv).rules


def test_home_forms_windows():
    assert one(r"~\.strateg\private\x", NT).strateg_home
    assert one("~/.strateg/private/x", NT).strateg_home
    assert one(r"%USERPROFILE%\.strateg\x", NT).strateg_home
    assert one(r"%userprofile%\.STRATEG\x", NT).strateg_home  # имена переменных на Windows без учёта регистра
    assert one("$env:USERPROFILE/.strateg/x", NT).strateg_home
    assert one("$HOME/.strateg/x", NT).strateg_home
    assert one(WIN_HOME + r"\.strateg", NT).strateg_home
    assert one("../.strateg/x", NT).strateg_home  # от корня продукта на уровень вверх — домашняя папка
    assert not one(r"~\strateg-notes", NT).strateg_home


@pytest.mark.parametrize("penv", [LINUX, MAC], ids=["linux", "macos"])
def test_home_forms_posix(penv):
    home = penv.home
    assert one("~/.strateg/private/x", penv).strateg_home
    assert one("$HOME/.strateg/x", penv).strateg_home
    assert one("${HOME}/.strateg/x", penv).strateg_home
    assert one(f"{home}/.strateg", penv).strateg_home
    assert one(f"{home}/Projects/../.strateg/x", penv).strateg_home
    assert not one(f"{home}/.strategy/x", penv).strateg_home


@ALL
def test_unresolvable_forms_return_none(penv):
    assert places("$NO_SUCH_VARIABLE_XYZ/x", penv) is None
    assert places("~someone/x", penv) is None
    assert places("a\x00b", penv) is None
    assert places("", penv) is None
    assert places("%NO_SUCH_VARIABLE_XYZ%/x", penv) is None


def test_windows_drive_forms():
    assert one(WIN_ROOT + r"\README.md", NT).in_root
    assert one(WIN_ROOT.lower() + r"\readme.md", NT).in_root
    assert one(WIN_ROOT.replace("C:", "c:").replace("\\", "/") + "/readme.md", NT).in_root
    assert not one(r"C:\Windows\System32\drivers\etc\hosts", NT).in_root
    assert not one(r"D:\other\file.txt", NT).in_root
    assert places("D:file.txt", NT) is None  # диск без черты зависит от текущей папки на диске


def test_windows_unc_and_extended_prefix():
    unc = one(r"\\server\share\evals\x", NT)
    assert not unc.in_root and not unc.evals
    assert one("//server/share/x", NT).path.anchor == "//server/share"
    ext = one("\\\\?\\" + WIN_ROOT + r"\evals\x", NT)  # \\?\C:\... — тот же путь
    assert ext.evals and ext.in_root
    assert one("\\\\?\\UNC\\server\\share\\x", NT).path.anchor == "//server/share"


def test_windows_trailing_dots_spaces_and_streams():
    assert one(r"projects\demo\inputs.\a.txt", NT).project_rel == ("inputs", "a.txt")
    assert one("projects\\demo\\inputs \\a.txt", NT).project_rel == ("inputs", "a.txt")
    assert one(r"evals.\x", NT).evals
    assert one(r"projects\demo\claims.yaml:stream", NT).project_rel == ("claims.yaml",)
    assert one(r"\.tmp\pending ", NT).pending


def test_posix_keeps_trailing_dots():
    assert one("projects/demo/inputs./a.txt", LINUX).project_rel == ("inputs.", "a.txt")


@ALL
def test_projects_id_must_look_like_an_id(penv):
    assert one("projects/demo-1/sources/a", penv).project_id == "demo-1"
    assert one("projects/.hidden/sources/a", penv).project_id is None
    assert one("projects/.active", penv).project_id is None


def test_paths_outside_everything():
    for penv, raw in ((NT, r"C:\Windows\win.ini"), (LINUX, "/etc/passwd"), (MAC, "/etc/hosts")):
        pl = one(raw, penv)
        assert not (pl.in_root or pl.in_projects or pl.in_claude_dir or pl.in_scratchpad)


def test_claude_dir_and_scratchpad_zones():
    pl = one(WIN_HOME + r"\.claude\projects\x\tool-results\a.txt", NT)
    assert pl.in_claude_dir and not pl.in_root
    assert not one(WIN_HOME + r"\.claude\.credentials.json", NT).in_claude_dir
    scratch = WIN_HOME + r"\AppData\Local\Temp\claude\abc\scratchpad"
    penv = dataclasses.replace(NT, scratchpad=scratch)
    assert one(scratch + r"\note.txt", penv).in_scratchpad
    assert not one(scratch + r"\..\other.txt", penv).in_scratchpad


# --- символические ссылки: проверка по настоящему пути -----------------------------------------------------


def fake_realpath(mapping: dict[str, str]):
    def real(raw: str) -> str:
        key = raw.replace("\\", "/")
        for src, dst in mapping.items():
            if key.lower().startswith(src.lower()):
                return dst + key[len(src) :]
        return raw

    return real


def test_symlink_pointing_into_protected_zone_is_caught():
    penv = dataclasses.replace(
        LINUX,
        realpath=fake_realpath(
            {"/home/ivan/strateg agent/projects/demo/sources/link": "/home/ivan/.strateg/private"}
        ),
    )
    found = places("projects/demo/sources/link/secret.txt", penv)
    assert found is not None and len(found) == 2
    assert not found[0].strateg_home  # по записи путь выглядит безобидно
    assert found[1].strateg_home  # а по настоящему пути он ведёт в ~/.strateg/


def test_symlink_out_of_the_working_folder_is_not_in_root():
    penv = dataclasses.replace(
        LINUX, realpath=fake_realpath({"/home/ivan/strateg agent/projects/demo/outputs/doc": "/etc"})
    )
    found = places("projects/demo/outputs/doc/passwd", penv)
    assert found is not None
    assert found[0].in_root and not found[1].in_root


def test_real_symlink_on_this_machine(tmp_path):
    """Настоящая ссылка на диске; на Windows без прав на ссылки тест пропускается."""
    import os

    root = tmp_path / "root"
    (root / "projects" / "demo" / "sources").mkdir(parents=True)
    secret = tmp_path / "home" / ".strateg" / "private"
    secret.mkdir(parents=True)
    link = root / "projects" / "demo" / "sources" / "link"
    try:
        os.symlink(secret, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("ссылки недоступны на этой машине")
    system = paths.penv_from_system(root=str(root), projects=str(root / "projects"), cwd=str(root), env={})
    penv = dataclasses.replace(system, home=str(tmp_path / "home"))
    found = places(str(link / "x.txt"), penv)
    assert found is not None and any(pl.strateg_home for pl in found)


# --- Glob: проверяется то, что стоит до первой маски --------------------------------------------------------


def test_glob_target():
    assert paths.glob_target("*.md", None, "/r") == "/r"
    assert paths.glob_target("*.md", "/r/knowledge", "/r") == "/r/knowledge"
    assert paths.glob_target("../../inputs/*", "/r/projects/demo/sources", "/r") == (
        "/r/projects/demo/sources/../../inputs"
    )
    assert paths.glob_target("/etc/**/*.conf", None, "/r") == "/etc"
    assert paths.glob_target("C:\\Work\\x\\*", None, "/r") == "C:/Work/x"
    assert paths.glob_target("~/.strateg/**", None, "/r") == "~/.strateg"
    assert paths.glob_target("evals/a.yaml", None, "/r") == "/r/evals/a.yaml"
    assert paths.glob_target("**/*.py", "/r/src", "/r") == "/r/src"
