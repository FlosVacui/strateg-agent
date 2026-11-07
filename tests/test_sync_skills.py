import pytest

import sync_skills


@pytest.fixture
def skills(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "SKILL.md").write_text("один", encoding="utf-8")
    (src / "a" / "extra.md").write_text("доп", encoding="utf-8")
    (src / ".gitkeep").write_text("", encoding="utf-8")
    monkeypatch.setattr(sync_skills, "SRC", src)
    return src


def test_unknown_flag_is_error(capsys):
    with pytest.raises(SystemExit) as exc:
        sync_skills.main(["--dest", "x", "--force"])
    assert exc.value.code == 2
    assert "--force" in capsys.readouterr().err


def test_without_flags_nothing_is_copied(capsys):
    with pytest.raises(SystemExit) as exc:
        sync_skills.main([])
    assert exc.value.code == 2


def test_dest_and_check_do_not_mix(tmp_path):
    with pytest.raises(SystemExit) as exc:
        sync_skills.main(["--dest", str(tmp_path / "x"), "--check"])
    assert exc.value.code == 2


def test_dest_makes_copy_in_given_folder(skills, tmp_path, capsys):
    dest = tmp_path / "out" / "skills"
    assert sync_skills.main(["--dest", str(dest)]) == 0
    assert (dest / "a" / "SKILL.md").read_text(encoding="utf-8") == "один"
    assert (dest / "a" / "extra.md").read_text(encoding="utf-8") == "доп"
    assert not (dest / ".gitkeep").exists()  # файлы вне папок скиллов в копию не идут
    assert "Скопировано файлов: 2" in capsys.readouterr().out


def test_dest_replaces_only_skill_folders(skills, tmp_path):
    dest = tmp_path / "out"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "old.md").write_text("старый", encoding="utf-8")
    (dest / "other").mkdir()
    (dest / "other" / "keep.md").write_text("чужой", encoding="utf-8")
    assert sync_skills.main(["--dest", str(dest)]) == 0
    assert not (dest / "a" / "old.md").exists()
    assert (dest / "a" / "SKILL.md").is_file()
    assert (dest / "other" / "keep.md").read_text(encoding="utf-8") == "чужой"


def test_dest_inside_source_is_refused(skills, capsys):
    assert sync_skills.main(["--dest", str(skills)]) == 1
    assert sync_skills.main(["--dest", str(skills / "a")]) == 1
    assert (skills / "a" / "SKILL.md").read_text(encoding="utf-8") == "один"
    assert "источник" in capsys.readouterr().err


def test_check_makes_copy_in_temp_folder_and_creates_nothing(skills, tmp_path, capsys):
    before = sorted(p.name for p in tmp_path.iterdir())
    assert sync_skills.main(["--check"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert "расхождений: 0" in capsys.readouterr().out


def test_check_fails_when_script_loses_a_file(skills, monkeypatch, capsys):
    real = sync_skills.copy_skills

    def lossy(dest):
        count = real(dest)
        (dest / "a" / "extra.md").unlink()
        return count

    monkeypatch.setattr(sync_skills, "copy_skills", lossy)
    assert sync_skills.main(["--check"]) == 1
    assert "расходится: a/extra.md" in capsys.readouterr().out


def test_check_fails_without_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_skills, "SRC", tmp_path / "none")
    assert sync_skills.main(["--check"]) == 1


def test_repository_skills_copy_matches_source(capsys):
    assert sync_skills.main(["--check"]) == 0
    assert "расхождений: 0" in capsys.readouterr().out
