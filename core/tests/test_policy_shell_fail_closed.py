"""Shell rules fail closed where shells split a command differently (TD-5156).

The differential test (``test_policy_shell_differential.py``) runs a corpus
under every shell on the host. These are the cases it found, written out
with real rules, plus the review notes on the same code: ``never`` and
``ask`` rules see a program named by its full path, and cmd's ``for /f``
runs the command in its parentheses. Each test fails on the TD-5101 code.
"""

from __future__ import annotations

import pytest

from tstd import policy_shell
from tstd.autonomy.classifier import DecisionClass
from tstd.policy import PolicyConfig, PolicyRule, resolve
from tstd.policy_shell import split_command
from tstd.tools import Tool

SHELL = Tool(name="shell")


def _rule(args: str, effect: str) -> PolicyRule:
    return PolicyRule.model_validate({"tool": "shell", "args": args, "effect": effect})


def _shell(command: str, *rules: PolicyRule) -> str:
    return resolve(PolicyConfig(rules=list(rules)), SHELL, {"command": command}, DecisionClass.B)


NPM_TEST = _rule("npm test*", "auto")
CAT = _rule("cat *", "auto")
EVERYTHING = _rule("**", "auto")
CURL_NEVER = _rule("curl *", "never")


# ── Line breaks the shells read differently ─────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        # bash reads the body from the lines after `)`; dash leaves it empty
        # and runs them.
        ": $(cat <<E) ; npm test\ncurl x\nE",
        'echo "$(cat <<E)"\ncurl x\nE',
        # bash joins `E\` and the empty line into the delimiter `E`.
        "cat <<E\nE\\\n\ncurl x\nE",
        "cat <<-E\n\tE\\\n\ncurl x\nE",
        # Inside $( ), bash ends the body at `E)` too.
        "(: $(cat <<E\nx\nE); curl x\nE\n)",
        # bash drops a body's `\`-newline first, so the comment swallows
        # `curl x`; dash runs it.
        "cat <<E\n$(true #\\\ncurl x\n)\nE",
        # `\`-newline joins `$` and `(`, `<<` and `<`, `&` and `&`.
        'echo "$\\\n(curl x)"',
        "cat <<E\n$\\\n(curl x)\nE",
        "cat <<\\\n<x\ncurl x",
        "npm test &\\\n& curl x",
        # `ca\`-newline-`se` is the keyword `case`.
        "echo $(ca\\\nse x in x) curl x;; esac)",
    ],
)
def test_text_shells_split_differently_never_auto_runs(command: str) -> None:
    assert split_command(command, ("posix",)).complete is False
    assert _shell(command, EVERYTHING) == "ask"
    assert _shell(command, EVERYTHING, CURL_NEVER) == "never"


@pytest.mark.parametrize(
    "command",
    [
        # Every shell drops `\`-newline before it reads words: these are a
        # comment, then `curl x` on its own line.
        "npm test \\\n# '\ncurl x\n'",
        "npm test; \\\n# it's\ncurl x\n'",
        # The delimiter is `EOF` in every shell, so `curl x` runs after it.
        "cat <<\\\nEOF\nhello\nEOF\ncurl x",
        "cat <<EO\\\nF\nhello\nEOF\ncurl x",
        "cat <<EOF\\\n\nhello\nEOF\ncurl x",
        'cat <<"EO\\\nF"\nhello\nEOF\ncurl x',
    ],
)
def test_a_line_continuation_does_not_hide_the_next_command(command: str) -> None:
    assert _shell(command, NPM_TEST, CAT) == "ask"
    assert _shell(command, NPM_TEST, CAT, CURL_NEVER) == "never"


def test_a_heredoc_before_a_substitution_keeps_its_body_after_the_line() -> None:
    # A newline inside `$( )` does not start the body: every shell runs
    # `curl x` and reads `y` as the body.
    command = "cat <<E $(\ncurl x\n)\ny\nE"
    assert split_command(command, ("posix",)).complete
    assert _shell(command, CURL_NEVER) == "never"


def test_everyday_continuations_and_heredocs_still_auto_run() -> None:
    lint = _rule("npm run lint*", "auto")
    assert _shell("npm test && \\\n  npm run lint", NPM_TEST, lint) == "auto"
    assert _shell("npm test \\\n  --silent", NPM_TEST) == "auto"
    script = "cat > run.sh <<EOF\ndocker run \\\n  --rm x\nEOF"
    assert _shell(script, _rule("cat > run.sh*", "auto")) == "auto"


# ── never and ask see a program named by its path ───────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/curl x",
        "/usr/local/bin/curl -s x | sh",
        "npm test; ./bin/../curl x",
        "sudo /usr/bin/curl x",
        "env FOO=1 /opt/curl/bin/curl x",
        "echo $(/usr/bin/curl x)",
    ],
)
def test_never_matches_a_program_named_by_its_full_path(command: str) -> None:
    assert _shell(command, EVERYTHING, CURL_NEVER) == "never"
    assert _shell(command, EVERYTHING, _rule("curl *", "ask")) == "ask"


def test_auto_rules_still_need_the_path_as_written() -> None:
    # Only never and ask read the basename: `npm test*` must not allow some
    # other program that happens to be called npm.
    assert _shell("/tmp/npm test", NPM_TEST) == "ask"


def test_a_windows_path_and_extension_reach_the_program_name() -> None:
    split = split_command("C:\\Tools\\curl.EXE x", ("cmd",))
    assert "curl x" in split.deny_texts


# ── Strings run later ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    ["alias ls='curl x'\nls", "trap 'curl x' EXIT", "trap -- 'curl x' INT TERM"],
)
def test_an_alias_value_and_a_trap_action_are_read_as_commands(command: str) -> None:
    assert _shell(command, EVERYTHING, CURL_NEVER) == "never"


# ── Windows: cmd's for /f runs the command in its parentheses ───────────


@pytest.mark.parametrize(
    "command",
    [
        "for /f \"tokens=*\" %i in ('curl x') do @echo %i",
        'for /F "usebackq tokens=*" %i in (`curl x`) do @echo %i',
        "FOR /f %%a IN ('curl x ^| findstr y') DO echo %%a",
    ],
)
def test_cmd_for_f_never_auto_runs_and_never_sees_its_command(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    split = split_command(command, ("cmd",))
    assert split.complete is False
    assert any(text.startswith("curl x") for text in split.deny_texts)
    monkeypatch.setattr(policy_shell, "host_dialects", lambda: ("posix", "cmd"))
    assert _shell(command, EVERYTHING) == "ask"
    assert _shell(command, EVERYTHING, CURL_NEVER) == "never"
