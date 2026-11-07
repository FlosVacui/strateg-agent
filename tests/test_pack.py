"""`strateg pack`: архив папки проекта без кэша, временных файлов, исходников с ПДн и секретов (С-14)."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from helpers import fact, journal_of, load_yaml, write_claims
from strateg import pack as packlib
from strateg.project_check import check_project

PHONE = "+7 (905) 123-45-67"
EMAIL = "kontrol.person@example.test"
NOTE_WITH_PII = f"Контакт: {PHONE}, почта {EMAIL}.\nПодписка стоит 199 рублей в месяц, это дорого.\n"


# Ключи собираются из частей: тест не должен сам стать находкой сканера секретов репозитория.
def fake_secret(name: str) -> str:
    return {
        "sk-ant": "sk" + "-ant-" + "api03-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7",
        "sk": "sk" + "-" + "proj1234567890ABCDEFGH12",
        "ghp": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "github_pat": "github" + "_pat_" + "11ABCDEFG0abcdefghij1234567890",
        "aws": "AK" + "IA" + "IOSFODNN7EXAMPLE",
        "xoxb": "xo" + "xb-" + "1234567890-abcdefghij",
        "xoxp": "xo" + "xp-" + "1234567890-abcdefghij",
        "private": "-----BEGIN " + "RSA PRIVATE KEY" + "-----",
        "openssh": "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----",
        "bare": "-----BEGIN " + "PRIVATE KEY" + "-----",
    }[name]


SECRET_LINES = {
    "sk-ant": f"ключ: {fake_secret('sk-ant')}",
    "sk": f"OPENAI={fake_secret('sk')}",
    "ghp": f"git push https://{fake_secret('ghp')}@example.com/repo.git",
    "github_pat": f"export GH={fake_secret('github_pat')}",
    "aws": f"aws_access_key_id = {fake_secret('aws')}",
    "xoxb": f"slack {fake_secret('xoxb')}",
    "xoxp": f"slack {fake_secret('xoxp')}",
    "private": fake_secret("private"),
    "openssh": fake_secret("openssh"),
    "bare": fake_secret("bare"),
    "password": "password=" + "Sup3rS3cretValue",
    "password-colon": 'password: "' + "Sup3rS3cretValue" + '"',
    "token": "token=" + "abcdef0123456789abcdef",
    "token-json": '{"token": "' + "abcdef0123456789abcdef" + '"}',
}


def unpack(archive: Path, folder: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        z.extractall(folder)
    return folder


def names(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as z:
        return set(z.namelist())


def archive_text(archive: Path) -> str:
    with zipfile.ZipFile(archive) as z:
        return "\n".join(z.read(n).decode("utf-8", errors="replace") for n in z.namelist())


@pytest.fixture
def work(started, cli, tmp_path, private):
    """Проект с источником, утверждением и заметкой."""
    (started / "inputs").mkdir(exist_ok=True)
    (started / "inputs" / "заметка.txt").write_text("Заметка про цену подписки", encoding="utf-8")
    assert cli("add", "заметка.txt")[0] == 0
    assert cli("claim", "add", "--file", write_claims(started / ".tmp", fact()))[0] == 0
    assert check_project(started) == []
    return started


# --- сквозной путь ------------------------------------------------------------------------------------


def test_pack_unpack_gives_the_same_status_and_a_clean_check(work, cli, tmp_path):
    before = json.loads(cli("status", "--json")[1])
    code, out, err = cli("pack", "--out", str(tmp_path / "out" / "demo.zip"))
    assert code == 0, err
    archive = tmp_path / "out" / "demo.zip"
    assert archive.is_file() and "упакован" in out
    folder = unpack(archive, tmp_path / "elsewhere") / "demo"
    code, out, err = cli("check", "--project", str(folder))
    assert code == 0 and "ошибок нет" in out, err
    after = json.loads(cli("status", "--json", "--project", str(folder))[1])
    assert after == before
    assert cli("status", "--brief", "--peek", "--project", str(folder))[1].count("\n") <= 10
    # продолжение не повторяет выполненного: журнал тот же
    assert journal_of(folder) == journal_of(work)
    assert load_yaml(folder / "claims.yaml") == load_yaml(work / "claims.yaml")


def test_archive_has_project_files_and_no_cache_or_temp(work, cli, tmp_path):
    (work / ".tmp" / "scratch.yaml").write_text("временное", encoding="utf-8")
    (work / ".cache").mkdir(exist_ok=True)
    (work / ".cache" / "fetch.json").write_text("{}", encoding="utf-8")
    (work / "inputs" / "лишнее.tmp").write_text("хвост записи", encoding="utf-8")
    (work / "inputs" / ".DS_Store").write_bytes(b"x")
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0
    got = names(tmp_path / "demo.zip")
    for name in ("state.yaml", "sources.yaml", "claims.yaml", "journal.jsonl", "cost.jsonl"):
        assert f"demo/{name}" in got, name
    assert "demo/sources/i-1.txt" in got and "demo/inputs/заметка.txt" in got
    assert "demo/inputs/заметка.txt.txt" in got
    assert not [n for n in got if "/.tmp" in n or "/.cache" in n or n.endswith((".tmp", ".DS_Store"))]
    assert all(n.startswith("demo/") for n in got)
    assert (work / ".tmp" / "scratch.yaml").is_file()  # проект не тронут


def test_archive_has_no_absolute_paths_and_is_utf8_lf(work, cli, tmp_path):
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0
    text = archive_text(tmp_path / "demo.zip")
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "/") not in text
    with zipfile.ZipFile(tmp_path / "demo.zip") as z:
        for n in z.namelist():
            if n.endswith((".yaml", ".jsonl")):
                data = z.read(n)
                assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf"), n


def test_default_archive_lies_next_to_the_projects(work, cli, home):
    code, out, err = cli("pack")
    assert code == 0, err
    assert (home / "demo.zip").is_file() and str(home / "demo.zip") in out


def test_pack_json(work, cli, tmp_path):
    data = json.loads(cli("pack", "--out", str(tmp_path / "a.zip"), "--json")[1])
    assert data["ok"] is True and data["files"] == len(names(tmp_path / "a.zip")) and data["bytes"] > 0


def test_archive_inside_the_project_folder_is_refused(work, cli):
    code, _, err = cli("pack", "--out", str(work / "inputs" / "demo.zip"))
    assert code == 1 and "внутрь папки проекта" in err
    assert not (work / "inputs" / "demo.zip").exists()


def test_pack_does_not_change_the_project(work, cli, tmp_path):
    from helpers import tree_hashes

    before = tree_hashes(work)
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0
    assert tree_hashes(work) == before


# --- проверка проекта до упаковки ---------------------------------------------------------------------


def test_pack_refuses_a_project_that_fails_check(work, cli, tmp_path):
    (work / "sources" / "i-1.txt").write_text("снимок подменили", encoding="utf-8")
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1 and "проверка проекта нашла ошибки" in err and "снимок изменён" in err
    assert not (tmp_path / "demo.zip").exists()


# --- секреты ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(SECRET_LINES))
def test_secret_blocks_the_archive_and_is_named_by_file_and_line(work, cli, tmp_path, kind):
    note = work / "inputs" / "заметки" / "пароли.txt"
    note.parent.mkdir()
    note.write_text("первая строка\nвторая строка\n" + SECRET_LINES[kind] + "\n", encoding="utf-8")
    code, out, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1 and out == ""
    assert "inputs/заметки/пароли.txt, строка 3: похоже на секрет" in err
    for word in SECRET_LINES[kind].split():
        assert len(word) < 16 or word not in err  # значение секрета в сообщение не попадает
    assert not (tmp_path / "demo.zip").exists()


def test_secret_in_the_journal_or_state_is_found_too(work, cli, tmp_path):
    cli("event", "error", "--message", "упало: token=" + "abcdef0123456789abcdef")
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1 and "journal.jsonl, строка" in err


@pytest.mark.parametrize(
    "line",
    [
        "password=${PASSWORD}",
        "token=<ваш токен здесь>",
        "password=short",
        "token=xxxxxxxxxxxxxxxx",
        "token = ****************",
        "max_tokens: 4096 и tokens=12345678901234567890",
        "task-force и risk-averse-long-token-text-123 и desk-1234567890-abcdefghijklmn",
        "sk-learn и sk-short",
        "акция AKIA-распродажа",
        "ghp_ без значения и xoxb- тоже",
        "-----BEGIN CERTIFICATE-----",
    ],
)
def test_ordinary_text_is_not_a_secret(work, cli, tmp_path, line):
    (work / "inputs" / "текст.txt").write_text(f"{line}\n", encoding="utf-8")
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 0, err


def test_scan_text_reports_pattern_names_not_values():
    hits = packlib.scan_text("a\n" + fake_secret("ghp") + "\npassword=" + "Sup3rS3cretValue")
    assert [n for n, _ in hits] == [2, 3]
    assert "ghp_" in hits[0][1] and "password" in hits[1][1]


def test_binary_files_are_not_scanned_for_secrets(work, cli, tmp_path):
    (work / "inputs" / "x.bin").write_bytes(b"\x00\x01" + fake_secret("ghp").encode())
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0


# --- gitleaks, если он есть ---------------------------------------------------------------------------


def fake_gitleaks(monkeypatch, leaks, calls=None, exit_code=1):
    monkeypatch.setattr(
        packlib.shutil, "which", lambda name: "gitleaks-for-tests" if name == "gitleaks" else None
    )

    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        report = Path(cmd[cmd.index("--report-path") + 1])
        root = Path(cmd[-1]) if cmd[1] == "dir" else Path(cmd[cmd.index("--source") + 1])
        report.write_text(
            json.dumps([{**leak, "File": str(root / leak["File"])} for leak in leaks]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, exit_code, b"", b"")

    monkeypatch.setattr(packlib.subprocess, "run", run)


def test_gitleaks_is_used_when_it_is_in_the_path(work, cli, tmp_path, monkeypatch):
    calls: list = []
    leak = {"File": "inputs/заметка.txt", "StartLine": 1, "RuleID": "generic-api-key"}
    fake_gitleaks(monkeypatch, [leak], calls)
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1 and "inputs/заметка.txt, строка 1: gitleaks: generic-api-key" in err
    assert calls and calls[0][0] == "gitleaks-for-tests" and "--redact" in calls[0]
    assert not (tmp_path / "demo.zip").exists()


def test_gitleaks_findings_in_temp_folders_do_not_matter(work, cli, tmp_path, monkeypatch):
    fake_gitleaks(monkeypatch, [{"File": ".tmp/черновик.yaml", "StartLine": 2, "RuleID": "generic-api-key"}])
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0


def test_gitleaks_with_no_findings_and_older_command_are_fine(work, cli, tmp_path, monkeypatch):
    calls: list = []
    fake_gitleaks(monkeypatch, [], calls, exit_code=0)
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0
    assert calls[0][1] == "dir"


def test_without_gitleaks_the_built_in_patterns_still_work(work, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(packlib.shutil, "which", lambda name: None)
    (work / "inputs" / "k.txt").write_text(SECRET_LINES["ghp"], encoding="utf-8")
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1 and "inputs/k.txt, строка 1" in err


# --- исходники с персональными данными --------------------------------------------------------------


def test_archive_never_holds_personal_data(work, cli, tmp_path, private):
    (work / "inputs" / "интервью.txt").write_text(NOTE_WITH_PII, encoding="utf-8")
    assert cli("add", "интервью.txt")[0] == 0
    # до подтверждения замен упаковка отказывает, исходник в .tmp/ в архив не попал бы в любом случае
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 1
    assert cli("add", "--confirm", "И-3")[0] == 0
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 0, err
    text = archive_text(tmp_path / "demo.zip")
    for secret in ("123-45-67", EMAIL, "kontrol.person"):
        assert secret not in text, secret
    assert "[телефон]" in text and "[почта]" in text
    assert not any("pending" in n or "private" in n for n in names(tmp_path / "demo.zip"))
    assert (private / "demo" / "И-3" / "интервью.txt").is_file()


def test_original_with_personal_data_left_in_the_project_blocks_the_archive(work, cli, tmp_path, private):
    original = work / "inputs" / "интервью.txt"
    original.write_text(NOTE_WITH_PII, encoding="utf-8")
    assert cli("add", "интервью.txt")[0] == 0
    assert cli("add", "--confirm", "И-3")[0] == 0
    (work / "inputs" / "резерв.txt").write_text(NOTE_WITH_PII, encoding="utf-8")  # человек вернул исходник
    code, _, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 1
    assert "inputs/резерв.txt: исходник материала И-3 с персональными данными" in err
    assert not (tmp_path / "demo.zip").exists()
    # исходник во временной папке архиву не мешает: её в архив не кладут
    (work / "inputs" / "резерв.txt").unlink()
    (work / ".tmp" / "копия.txt").write_text(NOTE_WITH_PII, encoding="utf-8")
    assert cli("pack", "--out", str(tmp_path / "demo.zip"))[0] == 0
    assert "kontrol.person" not in archive_text(tmp_path / "demo.zip")
