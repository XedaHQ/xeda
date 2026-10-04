"""Completion must emit shell code, register it, and return usable candidates."""

import os
import shlex
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import SHELLS, cli
from xeda.flow import registered_flows

from . import io_flows  # noqa: F401 - registers the declared test flows

#: Flags that keep a shell from reading the user's startup files, and a command that must work.
SHELL_PROBES = {
    "bash": ["--noprofile", "--norc", "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
    "zsh": ["-f", "-c", "print ok"],
    "fish": ["--no-config", "-c", "echo ok"],
}


def _require_shell(shell):
    """The shell's executable; a skip (a failure under XEDA_TESTS_REQUIRE_TOOLS=1) when it is
    missing, and a differently worded one when it is installed but cannot run."""
    executable = shutil.which(shell)
    reason = None
    if executable is None:
        reason = f"{shell} is not installed"
    else:
        try:
            probe = subprocess.run(
                [executable, *SHELL_PROBES[shell]],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={**os.environ, "HOME": os.devnull},
            )
        except (OSError, subprocess.SubprocessError) as e:
            reason = f"{shell} is installed but broken: {e}"
        else:
            if probe.returncode != 0 or not probe.stdout.strip():
                reason = f"{shell} is installed but broken: exit {probe.returncode}: {probe.stderr}"
            elif shell == "bash" and tuple(map(int, probe.stdout.strip().split("."))) < (4, 4):
                reason = "Click requires bash 4.4 or newer for completion"
    if reason:
        if os.environ.get("XEDA_TESTS_REQUIRE_TOOLS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return executable


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_shell_and_a_broken_one_are_reported_apart(shell, monkeypatch, tmp_path):
    broken = tmp_path / shell
    broken.write_text("#!/bin/sh\necho 'cannot start' >&2\nexit 3\n")
    broken.chmod(0o755)
    monkeypatch.setenv("XEDA_TESTS_REQUIRE_TOOLS", "1")
    real_which = shutil.which

    def which_as(found):
        return lambda name, *a, **k: found if name == shell else real_which(name, *a, **k)

    monkeypatch.setattr(shutil, "which", which_as(None))
    with pytest.raises(pytest.fail.Exception, match=f"{shell} is not installed"):
        _require_shell(shell)
    monkeypatch.setattr(shutil, "which", which_as(str(broken)))
    with pytest.raises(pytest.fail.Exception, match=f"{shell} is installed but broken: exit 3"):
        _require_shell(shell)
    monkeypatch.delenv("XEDA_TESTS_REQUIRE_TOOLS")
    with pytest.raises(pytest.skip.Exception, match="installed but broken"):
        _require_shell(shell)


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
    assert candidate in _complete(shell, words)


def _complete(shell, words):
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
    assert result.stderr == ""
    assert "Usage:" not in result.stdout and "\x1b[" not in result.stdout
    # no candidates is a lone newline; zsh prints a (type, value, description) line triple
    lines = [line for line in result.stdout.splitlines() if line]
    if shell == "zsh":
        return lines[1::3]
    return [line.partition(",")[2].partition("\t")[0] for line in lines]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("xeda run yosys_fpga+ne", ["yosys_fpga+nextpnr"]),
        ("xeda run yosys_fpga+nextpnr+", ["yosys_fpga+nextpnr+fpga_pack"]),
        ("xeda run --json yosys_fpga+nextpnr+fpga_pack+open", None),
        ("xeda run nextpnr.", ["nextpnr.config"]),
        ("xeda run openfpgaloader+", []),
        ("xeda run yosys_fpga+nextpnr+n", []),
    ],
)
def test_completion_requests_complete_flow_chains(shell, words, expected):
    values = _complete(shell, words)
    if expected is None:
        expected = [words.split()[-1] + "fpgaloader"]
    assert values == expected


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_of_a_single_flow_name_is_unchanged(shell):
    values = _complete(shell, "xeda run yosys_f")
    assert values == ["yosys_fpga"]


@pytest.mark.parametrize("shell", SHELLS)
def test_other_commands_still_complete_one_flow_and_refuse_a_chain_prefix(shell):
    assert "vivado_synth" in _complete(shell, "xeda list-settings vivado_")
    assert _complete(shell, "xeda list-settings yosys_fpga+ne") == []


#: The command, and what the shell then offers: a name that must be among the candidates, or the
#: exact list (empty: no followers, so nothing is offered and nothing is printed).
@pytest.fixture(autouse=True)
def only_the_product_and_chain_fixture_flows():
    """Other test modules register flows of their own in this process; completion offers every
    registered flow, so the lists asserted here are taken over the product's flows and this
    suite's chain fixtures alone."""
    before = registered_flows.copy()
    for key, (module, cls) in before.items():
        if not (cls.__module__.startswith("xeda.flows") or cls.__module__ == io_flows.__name__):
            del registered_flows[key]
    yield
    registered_flows.clear()
    registered_flows.update(before)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("words", "expected"),
    [
        # an output-qualified element completes to the outputs that lead somewhere
        ("xeda run __chain_producer.j", ["__chain_producer.json_a", "__chain_producer.json_b"]),
        # a qualified prefix filters the followers through that output; its ambiguity is gone
        (
            "xeda run __chain_producer.json_a+__chain_a",
            [
                "__chain_producer.json_a+__chain_aliased",
                "__chain_producer.json_a+__chain_ambiguous_consumer",
                "__chain_producer.json_a+__chain_ambiguous_default",
            ],
        ),
        # an unqualified prefix offers only the followers that need no qualification
        (
            "xeda run __chain_producer+__chain_a",
            ["__chain_producer+__chain_action", "__chain_producer+__chain_ambiguous_default"],
        ),
        # an alias prefix completes to the alias; the prefix typed before is kept verbatim
        ("xeda run __chain_simple_producer+sink", ["__chain_simple_producer+sink_alias"]),
        ("xeda run chain-source+sink", []),  # json_a/json_b is ambiguous: qualify first
        ("xeda run chain-source.json_a+sink", ["chain-source.json_a+sink_alias"]),
        # a flow cannot repeat, an action ends a chain, an undeclared flow runs alone
        ("xeda run __chain_simple_producer+__chain_simple_p", []),
        ("xeda run __chain_simple_producer+__chain_action+", []),
        ("xeda run __chain_undeclared+", []),
    ],
)
def test_completion_of_chains_of_declared_test_flows(shell, words, expected):
    assert _complete(shell, words) == expected


CHAIN_CASES = [
    ("xeda r", "run"),
    ("xeda run yosys_fpga+ne", ["yosys_fpga+nextpnr"]),
    ("xeda run yosys_fpga+nextpnr+", ["yosys_fpga+nextpnr+fpga_pack"]),
    ("xeda run yosys_fpga+nextpnr+fpga_pack+", ["yosys_fpga+nextpnr+fpga_pack+openfpgaloader"]),
    ("xeda run nextpnr.", ["nextpnr.config"]),
    ("xeda run openfpgaloader+", []),
    ("xeda run yosys_fpga+nextpnr+n", []),
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("setup", ["eval", "file"])
@pytest.mark.parametrize(("words", "candidate"), CHAIN_CASES)
def test_shell_loads_completion_and_completes_commands(shell, setup, words, candidate, tmp_path):
    """The installed entry point, in the real shell, by both setup routes."""
    executable = _require_shell(shell)
    tokens = words.split()

    env = os.environ.copy()
    # Exercise the installed entry point belonging to this test's Python environment.
    env["PATH"] = sysconfig.get_path("scripts") + os.pathsep + env.get("PATH", "")
    # the entry point under test is this environment's own, never another checkout's
    assert shutil.which("xeda", path=env["PATH"]) == str(
        Path(sysconfig.get_path("scripts")) / "xeda"
    )
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
COMP_WORDS=(@WORDS@)
COMP_CWORD=@LAST@
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
# Candidates without a description reach zsh through `compadd -a ARRAY`, the last argument.
_describe() { print -rl -- "${(@P)3}"; }
compadd() { print -rl -- "${(@P)${@: -1}}"; }
words=(@WORDS@)
CURRENT=@CURRENT@
_xeda_completion
"""
        )
        arguments = [executable, "-f", "-c", script, "xeda-test", str(source_file)]
    else:
        script = initialize + "\ncomplete --do-complete @QUOTED@\n"
        arguments = [executable, "--no-config", "-c", script, str(source_file)]

    substitutions = {
        "@WORDS@": " ".join(shlex.quote(token) for token in tokens),
        "@LAST@": str(len(tokens) - 1),
        "@CURRENT@": str(len(tokens)),
        "@QUOTED@": shlex.quote(words),
    }
    for placeholder, value in substitutions.items():
        arguments = [argument.replace(placeholder, value) for argument in arguments]
    result = subprocess.run(
        arguments, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    candidates = [
        line.split(":")[0].split("\t")[0]
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("complete ")  # bash's `complete -p xeda`
    ]
    if isinstance(candidate, list):
        assert candidates == candidate
    else:
        assert candidate in candidates
    assert "Usage:" not in result.stdout and "\x1b[" not in result.stdout


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
