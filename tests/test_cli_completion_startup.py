"""Fresh-process shell completion must stay light and follow Click's candidate protocol."""

import json
import os
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.python_compat

SHELLS = ("bash", "zsh", "fish")
BLOCKED_MODULES = (
    "xeda.design",
    "xeda.flow",
    "xeda.flow_runner",
    "xeda.flows",
    "xeda.cli_utils",
    "xeda.console",
    "xeda.introspect",
    "xeda.tool",
    "xeda.units",
    "click_extra",
    "coloredlogs",
    "pint",
    "pydantic",
    "rich",
)


def _complete(shell, words, *, cwd=None):
    """Run the installed Click completion protocol in a fresh interpreter."""
    tokens = words.split()
    current = "" if words.endswith(" ") else tokens[-1]
    word_index = len(tokens) if words.endswith(" ") else len(tokens) - 1
    env = os.environ.copy()
    env["_XEDA_COMPLETE"] = f"{shell}_complete"
    env["COMP_WORDS"] = words
    env["COMP_CWORD"] = current if shell == "fish" else str(word_index)
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "from xeda.cli import cli; cli()"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Usage:" not in result.stdout
    return result.stdout


def _candidates(shell, output):
    """Decode Click's shell-specific candidate record format."""
    lines = [line for line in output.splitlines() if line]
    if shell == "zsh":
        return lines[1::3]
    return [line.partition(",")[2].partition("\t")[0] for line in lines]


def _normal_cli(*args):
    """Run the ordinary entry point with no inherited completion request."""
    env = os.environ.copy()
    env.pop("_XEDA_COMPLETE", None)
    env.pop("_CLI_COMPLETE", None)
    env.pop("COMP_WORDS", None)
    env.pop("COMP_CWORD", None)
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _visible_options(help_output):
    unstyled = re.sub(r"\x1b\[[0-9;]*m", "", help_output)
    options_section = unstyled.split("Options:", 1)[1].split("Commands:", 1)[0]
    options = set()
    for line in options_section.splitlines():
        if line.lstrip().startswith("-"):
            label = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            options.update(re.findall(r"--[\w-]+", label))
    return options


def _modules_seen(shell, words, tmp_path):
    """Record imports after Click has handled one completion request."""
    return _completion_and_modules_seen(shell, words, tmp_path)[1]


def _completion_and_modules_seen(shell, words, tmp_path):
    """Capture completion records and imported modules from the same fresh process."""
    marker = tmp_path / "completion-imports.json"
    script = (
        "import json, sys\n"
        "try:\n"
        " from xeda.cli import cli\n"
        " cli()\n"
        "finally:\n"
        " open(sys.argv[1], 'w').write(json.dumps(sorted(sys.modules)))\n"
    )
    tokens = words.split()
    current = "" if words.endswith(" ") else tokens[-1]
    word_index = len(tokens) if words.endswith(" ") else len(tokens) - 1
    env = os.environ.copy()
    env["_XEDA_COMPLETE"] = f"{shell}_complete"
    env["COMP_WORDS"] = words
    env["COMP_CWORD"] = current if shell == "fish" else str(word_index)
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, str(marker)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Usage:" not in result.stdout
    return result.stdout, json.loads(marker.read_text())


def _assert_light_imports(modules):
    imported = [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in BLOCKED_MODULES)
    ]
    assert imported == []


@pytest.mark.parametrize("shell", SHELLS)
def test_root_command_completion_is_light_and_uses_current_command_names(shell, tmp_path):
    output = _complete(shell, "xeda ")
    candidates = _candidates(shell, output)
    assert {"run", "list-flows", "skill", "completion"} <= set(candidates)
    _assert_light_imports(_modules_seen(shell, "xeda ", tmp_path))


@pytest.mark.parametrize("shell", SHELLS)
def test_run_option_completion_before_a_flow_is_light(shell, tmp_path):
    output = _complete(shell, "xeda run --run-r")
    assert "--run-root" in _candidates(shell, output)
    _assert_light_imports(_modules_seen(shell, "xeda run --run-r", tmp_path))


@pytest.mark.parametrize("shell", SHELLS)
def test_plain_flow_completion_includes_canonical_name_and_ghdl_alias(shell, tmp_path):
    candidates = _candidates(shell, _complete(shell, "xeda run ghdl"))
    assert {"ghdl", "ghdl_sim"} <= set(candidates)
    _assert_light_imports(_modules_seen(shell, "xeda run ghdl", tmp_path))


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("xeda run yosys_fpga+ne", ["yosys_fpga+nextpnr"]),
        ("xeda run yosys_fpga.netlist+ne", ["yosys_fpga.netlist+nextpnr"]),
        ("xeda run nextpnr.", ["nextpnr.config"]),
        ("xeda run yosys_fpga++", []),
        # the direct followers come first, then the ones a link reaches through other flows
        (
            "xeda run yosys_fpga+",
            [
                "yosys_fpga+nextpnr",
                "yosys_fpga+vivado_impl",
                "yosys_fpga+fpga_pack",
                "yosys_fpga+openfpgaloader",
            ],
        ),
        ("xeda run yosys_fpga.netlist+f", ["yosys_fpga.netlist+fpga_pack"]),
        ("xeda run yosys_fpga+fpga_pack+", ["yosys_fpga+fpga_pack+openfpgaloader"]),
        ("xeda run nextpnr+", ["nextpnr+fpga_pack", "nextpnr+openfpgaloader"]),
        ("xeda run nextpnr+openfpgaloader+", []),
        # a flow that a link skips cannot come again
        ("xeda run yosys_fpga+fpga_pack+ne", []),
        ("xeda run yosys_fpga+openfpgaloader+fpga_pack", []),
    ],
)
@pytest.mark.parametrize("shell", SHELLS)
def test_builtin_chain_completion_is_exact_and_light(shell, words, expected, tmp_path):
    output, modules = _completion_and_modules_seen(shell, words, tmp_path)
    candidates = _candidates(shell, output)
    assert candidates == expected
    _assert_light_imports(modules)


@pytest.mark.parametrize("shell", SHELLS)
def test_nested_skill_command_completion_is_light(shell, tmp_path):
    output = _complete(shell, "xeda skill ")
    assert {"install", "reference"} <= set(_candidates(shell, output))
    _assert_light_imports(_modules_seen(shell, "xeda skill ", tmp_path))


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "words",
    ["xeda help ", "xeda help sk", "xeda skill help ", "xeda skill help i"],
)
def test_help_target_completion_matches_the_original_empty_result(shell, words):
    # Click prints a lone newline when the original command has no completion candidates.
    assert _complete(shell, words) == "\n"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("words", ["xeda help --se", "xeda skill help --se"])
def test_help_search_option_completes_like_the_original_cli(shell, words):
    candidates = _candidates(shell, _complete(shell, words))
    assert candidates == ["--search"]


@pytest.mark.parametrize(
    ("help_args", "equivalent_args"),
    [
        (("help",), ("--help",)),
        (("help", "skill"), ("skill", "--help")),
        (("skill", "help"), ("skill", "--help")),
    ],
)
def test_help_commands_match_normal_cli(help_args, equivalent_args):
    help_result = _normal_cli(*help_args)
    equivalent_result = _normal_cli(*equivalent_args)
    assert help_result.returncode == equivalent_result.returncode == 0
    assert help_result.stderr == equivalent_result.stderr == ""
    assert help_result.stdout == equivalent_result.stdout


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "words",
    [
        "xeda run ghdl_sim --run-r",
        "xeda run yosys_fpga+nextpnr+fpga_pack --run-r",
    ],
)
def test_run_options_after_a_complete_flow_or_chain_are_light(shell, words, tmp_path):
    output = _complete(shell, words)
    assert "--run-root" in _candidates(shell, output)
    _assert_light_imports(_modules_seen(shell, words, tmp_path))


@pytest.mark.parametrize("shell", SHELLS)
def test_directory_completion_preserves_shell_candidate_kind(shell, tmp_path):
    candidate = tmp_path / "candidate-dir"
    candidate.mkdir()
    words = f"xeda skill install --dir {tmp_path}/candidate"
    output = _complete(shell, words, cwd=tmp_path)
    # Click returns the directory kind and typed prefix. The shell enumerates the directory.
    if shell == "bash":
        assert output == f"dir,{tmp_path}/candidate\n"
    elif shell == "fish":
        assert output == f"dir,{tmp_path}/candidate\n"
    else:
        assert output == f"dir\n{tmp_path}/candidate\n_\n"


@pytest.mark.parametrize("shell", SHELLS)
def test_file_completion_preserves_shell_candidate_kind(shell, tmp_path):
    (tmp_path / "candidate-design.yaml").write_text("name: candidate\n")
    words = f"xeda run ghdl_sim {tmp_path}/candidate-design"
    output = _complete(shell, words, cwd=tmp_path)
    if shell == "bash":
        assert output == f"file,{tmp_path}/candidate-design\n"
    elif shell == "fish":
        assert output == f"file,{tmp_path}/candidate-design\n"
    else:
        assert output == f"file\n{tmp_path}/candidate-design\n_\n"


@pytest.mark.parametrize(
    ("words", "help_args"),
    [
        ("xeda --", ("--help",)),
        ("xeda run --", ("run", "--help")),
    ],
)
def test_plain_completion_visible_options_match_the_normal_cli(words, help_args):
    shell = "bash"
    completion = set(_candidates(shell, _complete(shell, words)))
    help_result = _normal_cli(*help_args)
    assert help_result.returncode == 0, help_result.stderr
    assert completion == _visible_options(help_result.stdout)


@pytest.mark.parametrize("shell", SHELLS)
def test_run_options_after_repeated_settings_are_preserved(shell):
    words = "xeda run ghdl_sim -s first.value=1 -s second.value=2 --run-r"
    assert "--run-root" in _candidates(shell, _complete(shell, words))
