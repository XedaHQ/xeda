"""The commands that the first documents show work when someone types them.

The README, the quickstart, the tutorial and the agent skill (`SKILL.md` and its references) teach
`xeda ...` commands against the `sqrt` example. A command that names a file or a setting that does
not exist, or that fails on the design it is shown with, sends the reader away. So each command is
extracted from the documents and run in `examples/vhdl/sqrt`, where the tutorial runs it:

* a query command (`list-flows`, `design-schema`, `completion`, ...) runs as it is;
* a simulation with an installed open-source simulator runs for real and must pass;
* every other `xeda run` is planned (`--dry-run`): the plan resolves the flow, the design, every
  setting and the chain, and needs no vendor tool;
* a placeholder design (`blinky.yaml`, `design.yaml`) stands for `sqrt.yaml`, and a command with a
  `<placeholder>` in it is a template that is not run.

The documents give no command that this test does not run unless `NOT_RUN` says why.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli

from . import tool_utils

ROOT = Path(__file__).parent.parent
SQRT_DIR = ROOT / "examples" / "vhdl" / "sqrt"
AGENT = ROOT / "src" / "xeda" / "data" / "agent"

DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "docs" / "quickstart.rst",
    ROOT / "docs" / "tutorial" / "index.rst",
    AGENT / "SKILL.md",
    *sorted((AGENT / "references").glob("*.md")),
]

#: designs the documents invent; each stands for the example the commands use
PLACEHOLDER_DESIGNS = {"blinky.yaml": "sqrt.yaml", "design.yaml": "sqrt.yaml"}

#: simulators that run for real when installed: they need no vendor tool
REAL_SIMULATORS = {
    "ghdl_sim": tool_utils.require_ghdl,
    "nvc": tool_utils.require_nvc_evidence,
    "verilator": tool_utils.require_verilator,
}

#: commands that no test can run here, by xeda subcommand, with the reason
NOT_RUN = {"dse": "a search runs the flow many times; the flows shown need Vivado"}


def _commands(path: Path) -> list[tuple[str, str]]:
    """Every `xeda ...` command of a document's code blocks as (`file:line`, text). A markdown
    code block is fenced; a reStructuredText one is indented."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found = []
    fenced = False
    number = 0
    while number < len(lines):
        line = lines[number]
        if path.suffix == ".md" and line.startswith("```"):
            fenced = not fenced
        elif (
            fenced if path.suffix == ".md" else line.startswith("    ")
        ) and line.strip().startswith("xeda "):
            first, text = number + 1, line.strip()
            while text.endswith("\\") and number + 1 < len(lines):
                number += 1
                text = text[:-1] + " " + lines[number].strip()
            found.append((f"{path.relative_to(ROOT).as_posix()}:{first}", text))
        number += 1
    return found


COMMANDS = [(where, text) for document in DOCUMENTS for where, text in _commands(document)]


def _words(text: str) -> list[str]:
    """The words of the `xeda` command before any pipe or redirection, without `xeda`."""
    words = shlex.split(text, comments=True)
    for index, word in enumerate(words):
        if word in ("|", ">", ">>"):
            words = words[:index]
            break
    assert words[0] == "xeda"
    return words[1:]


def test_the_documents_show_commands():
    assert len(COMMANDS) >= 40
    assert {Path(where.split(":")[0]).name for where, _ in COMMANDS} >= {
        "README.md",
        "quickstart.rst",
        "index.rst",
        "SKILL.md",
    }


@pytest.mark.parametrize("where, text", COMMANDS, ids=[f"{where}" for where, _ in COMMANDS])
def test_a_documented_command_works_where_it_is_shown(where, text, tmp_path, monkeypatch):
    words = _words(text)
    if any("<" in word for word in words):
        pytest.skip("a template with a placeholder")
    monkeypatch.chdir(SQRT_DIR)
    words = [PLACEHOLDER_DESIGNS.get(word, word) for word in words]
    if words[0] == "skill":
        monkeypatch.chdir(tmp_path)  # `skill install` writes into the working directory
    if words[0] in NOT_RUN:
        pytest.skip(NOT_RUN[words[0]])
    if words[0] == "run":
        flow = words[1]
        words += ["--run-root", str(tmp_path / "xeda_run")]
        if flow in REAL_SIMULATORS:
            REAL_SIMULATORS[flow]()
            tool_utils.require_cocotb()
        elif "--dry-run" not in words:
            words.append("--dry-run")
    result = CliRunner().invoke(cli, words)
    assert result.exit_code == 0, f"{where}: xeda {' '.join(words)}\n{result.output}"
