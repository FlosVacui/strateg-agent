"""`scripts/check_all.py` повторяет CI: команды из `.github/workflows/ci.yml` и проверку адресов."""

from __future__ import annotations

from pathlib import Path

import yaml

import check_all

ROOT = Path(__file__).resolve().parent.parent
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def ci_commands() -> list[str]:
    return [step["run"].strip() for step in CI["jobs"]["test"]["steps"] if "run" in step]


def test_local_steps_are_the_ci_steps_in_the_same_order():
    assert [" ".join(argv) for _, argv in check_all.STEPS] == ci_commands()


def test_the_secrets_job_is_covered_too():
    secrets = CI["jobs"]["secrets"]["steps"]
    assert any("gitleaks" in step.get("uses", "") for step in secrets)
    assert any("noreply.github.com" in step.get("run", "") for step in secrets)
    assert check_all.SERVICE_DOMAIN == "@users.noreply.github.com"


def test_personal_address_is_found_and_service_ones_pass():
    log = "\n".join(
        [
            "a1b2c3d 1+name@users.noreply.github.com noreply@github.com",
            "d4e5f6a person@example.test 1+name@users.noreply.github.com",
            "0a0b0c0 1+name@users.noreply.github.com other@example.test",
        ]
    )
    assert check_all.bad_author_addresses(log) == ["d4e5f6a", "0a0b0c0"]


def test_no_tests_flag_skips_only_pytest(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(check_all, "run", lambda argv: (calls.append(argv), (0, ""))[1])
    monkeypatch.setattr(check_all, "gitleaks", lambda: (None, "нет"))
    monkeypatch.setattr(check_all, "author_addresses", lambda: (0, "ок"))
    assert check_all.main(["--no-tests"]) == 0
    assert ["uv", "run", "pytest", "-q"] not in calls
    assert len(calls) == len(check_all.STEPS) - 1
    out = capsys.readouterr().out
    assert "пропущено Тесты (--no-tests)" in out


def test_failed_step_gives_code_1_and_shows_its_output(monkeypatch, capsys):
    def fake(argv):
        return (1, "ruff нашёл ошибку") if argv[2] == "ruff" and "check" in argv else (0, "")

    monkeypatch.setattr(check_all, "run", fake)
    monkeypatch.setattr(check_all, "gitleaks", lambda: (0, ""))
    monkeypatch.setattr(check_all, "author_addresses", lambda: (0, ""))
    assert check_all.main(["--no-tests"]) == 1
    out = capsys.readouterr().out
    assert "ruff нашёл ошибку" in out and "Не прошло: Ruff — ошибки" in out
