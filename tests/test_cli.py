import pytest

from strateg import __version__, cli
from strateg.cli import main


def test_version_constant():
    assert __version__.count(".") == 2


def test_help_without_command(capsys):
    assert main([]) == 0
    assert "strateg" in capsys.readouterr().out


def _utf8_console(monkeypatch):
    """Подменяет определение консоли на «всё в порядке», чтобы тест не зависел от машины."""
    monkeypatch.setattr(cli, "_windows_console_cp", lambda: 65001)
    monkeypatch.setattr(cli, "_stdout_encoding", lambda: "utf-8")


def test_doctor_strict_ok(capsys, tmp_path, monkeypatch):
    """В нормальной среде doctor возвращает 0 и не зависит от текущей папки."""
    _utf8_console(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Python" in out and "Запись в projects/" in out
    assert "НЕТ" not in out


def test_doctor_checks_projects_not_cwd(capsys, tmp_path, monkeypatch):
    _utf8_console(monkeypatch)
    seen = []
    monkeypatch.setattr(
        cli, "_check_writable", lambda folder: (seen.append(folder), (cli.OK, str(folder)))[1]
    )
    monkeypatch.chdir(tmp_path)
    assert main(["doctor"]) == 0
    assert [p.name for p in seen] == ["projects"]
    assert tmp_path not in seen[0].parents


def test_doctor_unwritable_projects_is_error(capsys, monkeypatch):
    _utf8_console(monkeypatch)
    monkeypatch.setattr(cli, "_check_writable", lambda folder: (cli.ERR, f"{folder} (отказано)"))
    assert main(["doctor"]) == 1
    assert "НЕТ" in capsys.readouterr().out


@pytest.mark.parametrize("cp", [866, 1251])
def test_doctor_catches_wrong_windows_codepage(capsys, monkeypatch, cp):
    """Кодовая страница cp866/cp1251 — предупреждение, даже когда Python уже перешёл на utf-8."""
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_windows_console_cp", lambda: cp)
    assert main(["doctor"]) == 0  # предупреждение код не меняет
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "Кодировка консоли" in x)
    assert line.startswith("[ПРЕД]")
    assert str(cp) in line and "chcp 65001" in line and "Windows Terminal" in line


@pytest.mark.parametrize(("cp", "level"), [(65001, cli.OK), (0, cli.OK), (866, cli.WARN)])
def test_detect_windows_codepage(monkeypatch, cp, level):
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_windows_console_cp", lambda: cp)
    assert cli.detect_console_encoding()[0] == level


def test_detect_posix_encoding(monkeypatch):
    monkeypatch.setattr(cli, "_is_windows", lambda: False)
    monkeypatch.setattr(cli, "_stdout_encoding", lambda: "cp866")
    assert cli.detect_console_encoding()[0] == cli.WARN
    monkeypatch.setattr(cli, "_stdout_encoding", lambda: "UTF-8")
    assert cli.detect_console_encoding()[0] == cli.OK


def test_encoding_detected_before_reconfigure(monkeypatch):
    """main() определяет кодировку раньше, чем перенастраивает stdout."""
    order = []
    monkeypatch.setattr(cli, "detect_console_encoding", lambda: (order.append("detect"), (cli.OK, "x"))[1])

    class Out:
        encoding = "cp866"

        def reconfigure(self, **_):
            order.append("reconfigure")

        def write(self, _):
            return 0

        def flush(self):
            pass

    monkeypatch.setattr(cli.sys, "stdout", Out())
    main([])
    assert order[:2] == ["detect", "reconfigure"]


def test_doctor_without_git_is_warning(capsys, monkeypatch):
    _utf8_console(monkeypatch)
    real_which = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "git" else real_which(name))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "git" in x and x.startswith("["))
    assert line.startswith("[ПРЕД]")


def test_doctor_shows_sqlite_version_and_gitleaks(capsys, monkeypatch):
    """ADR-0011: `strateg doctor` показывает версию SQLite и наличие gitleaks."""
    import sqlite3

    _utf8_console(monkeypatch)
    real_which = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/opt/bin/gitleaks" if name == "gitleaks" else real_which(name)
    )
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert any(x.startswith("[OK  ] SQLite") and sqlite3.sqlite_version in x for x in out.splitlines())
    assert any(x.startswith("[OK  ] gitleaks") and "/opt/bin/gitleaks" in x for x in out.splitlines())


def test_doctor_without_gitleaks_is_a_warning_with_the_consequence(capsys, monkeypatch):
    _utf8_console(monkeypatch)
    real_which = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "gitleaks" else real_which(name))
    assert main(["doctor"]) == 0  # предупреждение код не меняет
    line = next(x for x in capsys.readouterr().out.splitlines() if "gitleaks" in x and x.startswith("["))
    assert line.startswith("[ПРЕД]") and "встроенными шаблонами" in line


def test_doctor_without_sqlite_module_is_a_warning(capsys, monkeypatch):
    import builtins

    _utf8_console(monkeypatch)
    real_import = builtins.__import__

    def no_sqlite(name, *args, **kwargs):
        if name == "sqlite3":
            raise ImportError("нет")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sqlite)
    assert main(["doctor"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if "SQLite" in x and x.startswith("["))
    assert line.startswith("[ПРЕД]")
