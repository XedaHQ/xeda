"""Completion must emit shell code, register it, and return usable candidates."""

import os
import shutil
import subprocess
import sysconfig

import pytest
from click.testing import CliRunner

from xeda.cli import SHELLS, cli


def _require_shell(shell):
    executable = shutil.which(shell)
    reason = None
    if executable is None:
        reason = f"{shell} is not installed"
    elif shell == "bash":
        version = subprocess.run(
            [
                executable,
                "--noprofile",
                "--norc",
                "-c",
                'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"',
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if tuple(map(int, version.stdout.strip().split("."))) < (4, 4):
            reason = "Click requires bash 4.4 or newer for completion"
    if reason:
        if os.environ.get("XEDA_TESTS_REQUIRE_TOOLS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return executable


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_source_environment(shell):
    if shell == "bash":
        _require_shell(shell)
    result = CliRunner().invoke(cli, [], env={"_XEDA_COMPLETE": f"{shell}_source"})
    assert result.exit_code == 0, result.output
    assert "_XEDA_COMPLETE=" + shell + "_complete" in result.stdout
    assert "Usage:" not in result.stdout
    assert "\x1b[" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_stdout_matches_environment_source(shell):
    if shell == "bash":
        _require_shell(shell)
    runner = CliRunner()
    source = runner.invoke(cli, [], env={"_XEDA_COMPLETE": f"{shell}_source"})
    explicit = runner.invoke(cli, ["completion", shell, "--stdout"])
    assert explicit.exit_code == source.exit_code == 0
    assert explicit.stdout == source.stdout
    assert explicit.stderr == ""


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("words", "candidate"),
    [
        ("xeda r", "run"),
        ("xeda list-settings vivado_", "vivado_synth"),
        ("xeda run --json --run-r", "--run-root"),
    ],
)
def test_completion_requests_return_candidates(shell, words, candidate):
    tokens = words.split()
    result = CliRunner().invoke(
        cli,
        [],
        env={
            "_XEDA_COMPLETE": f"{shell}_complete",
            "COMP_WORDS": words,
            "COMP_CWORD": tokens[-1] if shell == "fish" else str(len(tokens) - 1),
        },
    )
    assert result.exit_code == 0, result.output
    values = result.stdout.splitlines()
    if shell != "zsh":
        values = [line.partition(",")[2].partition("\t")[0] for line in values]
    assert candidate in values
    assert result.stderr == ""


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("setup", ["eval", "file"])
def test_shell_loads_completion_and_completes_commands(shell, setup, tmp_path):
    executable = _require_shell(shell)

    env = os.environ.copy()
    # Exercise the installed entry point belonging to this test's Python environment.
    env["PATH"] = sysconfig.get_path("scripts") + os.pathsep + env.get("PATH", "")
    env.pop("_XEDA_COMPLETE", None)
    env.pop("_CLI_COMPLETE", None)
    env.pop("FPATH", None)
    env["HOME"] = str(tmp_path)
    env["NO_COLOR"] = "1"
    source_file = tmp_path / "completion"
    if setup == "file":
        result = subprocess.run(
            ["xeda", "completion", shell, "--stdout"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert result.stderr == ""
        source_file.write_text(result.stdout)
        initialize = 'source "$1"' if shell != "fish" else 'source "$argv[1]"'
    else:
        # Run exactly the setup command printed by `xeda completion`.
        initialize = SHELLS[shell]["eval"]

    if shell == "bash":
        script = initialize + """
complete -p xeda
COMP_WORDS=(xeda r)
COMP_CWORD=1
_xeda_completion xeda
printf '%s\\n' "${COMPREPLY[@]}"
"""
        arguments = [
            executable,
            "--noprofile",
            "--norc",
            "-c",
            script,
            "xeda-test",
            str(source_file),
        ]
    elif shell == "zsh":
        # Ignore site/user completion files and dumps unrelated to Xeda. The directory
        # containing compinit also supplies compdef and the other initialization helpers.
        script = (
            "fpath=(${^fpath}/compinit(N:h)); autoload -Uz compinit; compinit -D\n"
            + initialize
            + """
[[ ${_comps[xeda]} == _xeda_completion ]] || exit 1
# Capture the candidates passed to zsh's renderer without needing an interactive ZLE.
_describe() { print -rl -- "${(@P)3}"; }
words=(xeda r)
CURRENT=2
_xeda_completion
"""
        )
        arguments = [executable, "-f", "-c", script, "xeda-test", str(source_file)]
    else:
        script = initialize + "\ncomplete --do-complete 'xeda r'\n"
        arguments = [executable, "--no-config", "-c", script, str(source_file)]

    result = subprocess.run(
        arguments, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert any(line.split(":")[0].split("\t")[0] == "run" for line in result.stdout.splitlines())
    assert "Usage:" not in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_instructions_are_printed_without_wrapping(shell, monkeypatch):
    from xeda.console import console

    monkeypatch.setattr(console, "width", 30)
    result = CliRunner().invoke(cli, ["completion", shell], env={"SHELL": f"/bin/{shell}"})
    assert result.exit_code == 0
    assert SHELLS[shell]["eval"] in result.stdout


def test_completion_requires_a_supported_shell(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    result = CliRunner().invoke(cli, ["completion"])
    assert result.exit_code == 2
    assert "shell" in result.stderr.lower()
    assert not isinstance(result.exception, AssertionError)


def test_completion_rejects_an_unsupported_default_shell():
    result = CliRunner().invoke(cli, ["completion"], env={"SHELL": "/bin/tcsh"})
    assert result.exit_code == 2
    assert "tcsh" in result.stderr
