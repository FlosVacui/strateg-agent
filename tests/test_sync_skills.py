import pytest

import sync_skills


def test_unknown_flag_is_error(capsys):
    with pytest.raises(SystemExit) as exc:
        sync_skills.main(["--force"])
    assert exc.value.code == 2
    assert "--force" in capsys.readouterr().err


def test_unknown_flag_does_not_touch_copy(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (dst / "keep.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sync_skills, "SRC", src)
    monkeypatch.setattr(sync_skills, "DST", dst)
    with pytest.raises(SystemExit):
        sync_skills.main(["sync"])
    assert (dst / "keep.txt").exists()


def test_check_and_sync(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "a").mkdir(parents=True)
    (src / "a" / "SKILL.md").write_text("один", encoding="utf-8")
    monkeypatch.setattr(sync_skills, "SRC", src)
    monkeypatch.setattr(sync_skills, "DST", dst)
    assert sync_skills.main(["--check"]) == 1  # копии ещё нет
    assert not dst.exists()  # проверка ничего не создаёт
    assert sync_skills.main([]) == 0
    assert (dst / "a" / "SKILL.md").read_text(encoding="utf-8") == "один"
    assert sync_skills.main(["--check"]) == 0
    (src / "a" / "SKILL.md").write_text("два", encoding="utf-8")
    assert sync_skills.main(["--check"]) == 1
