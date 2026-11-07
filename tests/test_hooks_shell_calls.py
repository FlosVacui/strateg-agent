"""Разбор Bash: адрес без кавычек, перевод строки, регистр, флаги `uv run`, место команды, `evals/`."""

from __future__ import annotations

import pytest

from strateg.hooks.shell import fetch_command, network_reason, sensitive_mention

# --- адрес без кавычек: ни одного знака, который оболочка поймёт по-своему ---


@pytest.mark.parametrize(
    "char", [";", "|", "&", "`", "$", "<", ">", "\n", "\r", "\t", "(", ")", "*", "?", "#"]
)
def test_unquoted_url_refuses_every_shell_character(char):
    assert not fetch_command(f"uv run strateg fetch https://e.com/a{char}b").ok
    assert not fetch_command(f"uv run strateg fetch https://e.com/a{char}b --region RU").ok
    if not char.isspace():  # пробельный знак в конце команды отрезается, а команда остаётся целой
        assert not fetch_command(f"uv run strateg fetch https://e.com/a{char}").ok


def test_unquoted_url_with_safe_characters_is_allowed():
    assert fetch_command("uv run strateg fetch https://e.com/a-b_c.d~e%20f:8080/x=1+2@y").ok


@pytest.mark.parametrize("char", [";", "|", "&", "`", "$", "<", ">"])
def test_quoted_url_may_contain_shell_characters(char):
    assert fetch_command(f"uv run strateg fetch 'https://e.com/a{char}b'").ok


# --- перевод строки между сегментами и регистр охраны ---


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_newline_between_segments_does_not_hide_fetch(separator):
    command = f"uv run strateg status{separator}uv run strateg fetch https://x.com"
    assert network_reason(command) == "`strateg fetch`"


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_newline_between_segments_does_not_hide_a_network_program(separator):
    assert network_reason(f"ls{separator}curl https://x.com") == "curl"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("type .ENV", ".env"),
        ("cat .Env.Local", ".env"),
        ("cat .Strateg/x", "~/.strateg/"),
        ("dir ~/.STRATEG", "~/.strateg/"),
        ("type projects\\demo\\.TMP\\Pending\\a.txt", ".tmp/pending/"),
    ],
)
def test_sensitive_mentions_ignore_case(command, expected):
    assert sensitive_mention(command) == expected


# --- strateg fetch при любых флагах uv run ---


@pytest.mark.parametrize(
    "command",
    [
        "uv run --with-requirements r.txt strateg fetch x",
        "uv run --index-url http://x strateg fetch x",
        "uv run --config-file c.toml strateg fetch x",
        "uv run --frozen strateg fetch x",
        "uv run --project . strateg fetch x",
        "uv run -p 3.12 strateg fetch x",
        "uv run --env-file e strateg fetch x",
        "uv run --some-new-flag value strateg fetch x",
        "uv --quiet run --offline strateg fetch x",
        "uv run --with-requirements r.txt python -m strateg fetch x",
    ],
)
def test_strateg_fetch_is_found_under_any_uv_flags(command):
    assert network_reason(command) == "`strateg fetch`"


@pytest.mark.parametrize(
    "command",
    [
        "uv run --index-url http://x strateg status",
        "uv run --with-requirements r.txt strateg claim list",
    ],
)
def test_other_strateg_commands_stay_open_under_unknown_uv_flags(command):
    assert network_reason(command) is None


@pytest.mark.parametrize(
    "command,found",
    [
        ("uv run curl https://x.com strateg", "curl"),
        ("uv run --index-url http://x curl https://x.com strateg", "curl"),
        (
            "uv run python -c 'import urllib.request' strateg",
            "код с сетевым модулем в `python -c` или подобной команде",
        ),
    ],
)
def test_the_word_strateg_does_not_hide_a_network_program(command, found):
    assert network_reason(command) == found


# --- сетевая программа ищется на месте команды, а не среди аргументов ---


@pytest.mark.parametrize(
    "command",
    [
        "grep -r curl docs/",
        "ls | grep curl",
        'echo "wget"',
        "echo wget",
        "Select-String -Pattern wget -Path x",
        "git log --grep curl",
        "git commit -m 'fix curl parsing'",
        "cat docs/curl.md",
        "rg iwr src",
        "sed -n 1p wget.txt",
        "man ssh",
        "FOO=1 grep -n nc file.txt",
    ],
)
def test_network_program_name_as_an_argument_is_not_a_call(command):
    assert network_reason(command) is None


@pytest.mark.parametrize(
    "command,found",
    [
        ("curl https://x.com", "curl"),
        ("FOO=1 curl https://x.com", "curl"),
        ("ls; curl https://x.com", "curl"),
        ("ls && wget x", "wget"),
        ("ls | xargs curl", "curl"),
        ("env curl x", "curl"),
        ("sudo -u root curl x", "curl"),
        ("timeout 5 curl x", "curl"),
        ("time wget x", "wget"),
        ("nohup curl x &", "curl"),
        ("/usr/bin/curl x", "curl"),
        ("C:\\Windows\\System32\\curl.exe x", "curl"),
        ("echo $(curl x)", "curl"),
        ("echo `wget x`", "wget"),
        ("(curl x)", "curl"),
        ("bash -c 'curl x'", "curl"),
        ('powershell -Command "irm http://x"', "irm"),
        ("cmd /c curl x", "curl"),
        ("certutil -urlcache -f https://x/a a", "certutil -urlcache"),
        ("start https://example.com", "start <адрес>"),
    ],
)
def test_network_program_at_command_position_is_still_found(command, found):
    assert network_reason(command) == found


# --- evals/ закрыт и в тексте команды ---


@pytest.mark.parametrize(
    "command",
    [
        "cat evals/cases/fraud-protection/expected.md",
        "grep -r x evals",
        "Get-Content evals\\cases\\x",
        "git show HEAD:evals/cases/fraud-protection/expected.md",
        "git show main~3:evals/cases/x/expected.md",
        "git show abc1234:./evals/README.md",
        "git grep -n x -- evals",
        "git grep -n x HEAD -- evals/",
        "git log -p -- evals/cases/x",
        "ls ./evals",
        "type EVALS\\README.md",
        "cat 'evals/cases/x'",
        "cat tests/../evals/x",
        "cp evals/a.md /tmp/a.md",
    ],
)
def test_evals_is_refused_in_a_command_text(command):
    assert sensitive_mention(command) == "evals/"


@pytest.mark.parametrize(
    "command",
    [
        "cat docs/evals-notes.md",
        "ls retrievals",
        "git commit -m 'about evaluation'",
        "uv run strateg status",
        "grep -r curl docs/",
        "cat revals/x",
    ],
)
def test_harmless_evals_lookalikes(command):
    assert sensitive_mention(command) is None


def test_evals_mention_can_be_left_out_for_environments_without_roles():
    assert sensitive_mention("cat evals/x", evals=False) is None
    assert sensitive_mention("cat .env", evals=False) == ".env"
