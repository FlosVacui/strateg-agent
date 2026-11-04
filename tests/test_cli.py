from strateg import __version__
from strateg.cli import main


def test_version_constant():
    assert __version__.count(".") == 2


def test_help_without_command(capsys):
    assert main([]) == 0
    assert "strateg" in capsys.readouterr().out


def test_doctor_runs(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert "Python" in out and "Запись в папку" in out
    assert code in (0, 1)
