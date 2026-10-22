"""The commands that the first documents show work when someone types them.

The README, the quickstart, the tutorial and the agent skill (`SKILL.md` and its references) teach
`xeda ...` commands against the `sqrt` example. A command that names a file or a setting that does
not exist, or that fails on the design it is shown with, sends the reader away. So each command is
extracted from the documents and run in the root directory of the repository, where the documents
show them:

* a query command (`list-flows`, `design-schema`, `completion`, ...) runs as it is;
* a simulation with an installed open-source simulator runs for real and must pass;
* every other `xeda run` is planned (`--dry-run`): the plan resolves the flow, the design, every
  setting and the chain, and needs no vendor tool;
* a placeholder design (`blinky.yaml`, `design.yaml`) stands for `sqrt.yaml`, and a command with a
  `<placeholder>` in it is a template that is not run.

The documents give no command that this test does not run unless `NOT_RUN` says why.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import Design
from xeda.introspect import results_info

from . import tool_utils

ROOT = Path(__file__).parent.parent
SQRT_DIR = ROOT / "examples" / "vhdl" / "sqrt"
SQRT_FILE = "examples/vhdl/sqrt/sqrt.yaml"
AGENT = ROOT / "src" / "xeda" / "data" / "agent"

DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "docs" / "quickstart.rst",
    ROOT / "docs" / "tutorial" / "index.rst",
    ROOT / "docs" / "run-directories.rst",
    AGENT / "SKILL.md",
    *sorted((AGENT / "references").glob("*.md")),
]

#: designs the documents invent; each stands for the example the commands use
PLACEHOLDER_DESIGNS = {"blinky.yaml": SQRT_FILE, "design.yaml": SQRT_FILE}

#: what a template stands for: a flow whose settings and results the documents may name
TEMPLATE_WORDS = {"<flow>": "vivado_synth", "<design>": SQRT_FILE}

#: a documented pipeline (`where` of its command) that this test cannot check, with the reason;
#: every other pipeline has its `jq` paths checked
UNCHECKED_PIPELINES: dict[str, str] = {}

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


def _jq_filter(text: str) -> str | None:
    """The filter of the `jq` that the command is piped into, if there is one."""
    words = shlex.split(text, comments=True)
    if "|" not in words:
        return None
    after = words[words.index("|") + 1 :]
    assert after[0] == "jq", f"a pipeline into {after[0]} is not checked: {text}"
    return next(word for word in after[1:] if not word.startswith("-"))


JQ_PATH = re.compile(r"(?<![\w\]])((?:\.[A-Za-z_]\w*|\[\d*\])+)")
JQ_STEP = re.compile(r"\.([A-Za-z_]\w*)|\[(\d*)\]")


def _missing_in(document: Any, path: str) -> str | None:
    """Why the simple `jq` path (`.a.b`, `.a[0].b`, `.a[].b`) finds nothing in `document`, or None
    when every value it reaches has the next step."""
    nodes = [document]
    walked = ""
    for key, index in JQ_STEP.findall(path):
        walked += f".{key}" if key else f"[{index}]"
        following = []
        for node in nodes:
            if key:
                if not isinstance(node, dict) or key not in node:
                    return f"{walked} is not in the document"
                following.append(node[key])
            elif index != "":  # "0" is an index too; only "[]" walks every element
                if not isinstance(node, list) or len(node) <= int(index):
                    return f"{walked} is not in the document"
                following.append(node[int(index)])
            else:
                if not isinstance(node, list) or not node:
                    return f"{walked} has no elements in the document"
                following.extend(node)
        nodes = following
    return None


def _results_keys(flow: str) -> set[str]:
    """The metric paths that `xeda list-results` documents for a flow."""
    return {metric["path"] for metric in results_info(flow)["metrics"]}


@pytest.fixture(scope="module")
def run_document(tmp_path_factory) -> dict:
    """The `--json` document of a real run, which shows the shape of every run's."""
    tool_utils.require_ghdl()
    tool_utils.require_cocotb()
    run_root = tmp_path_factory.mktemp("document") / "xeda_run"
    result = CliRunner().invoke(
        cli, ["run", "ghdl_sim", str(SQRT_DIR / "sqrt.yaml"), "--run-root", str(run_root), "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_the_documents_show_commands():
    assert len(COMMANDS) >= 40
    assert {Path(where.split(":")[0]).name for where, _ in COMMANDS} >= {
        "README.md",
        "quickstart.rst",
        "index.rst",
        "SKILL.md",
    }


@pytest.mark.parametrize("where, text", COMMANDS, ids=[f"{where}" for where, _ in COMMANDS])
def test_a_documented_command_works_where_it_is_shown(where, text, tmp_path, monkeypatch, request):
    words = [TEMPLATE_WORDS.get(word, word) for word in _words(text)]
    if any("<" in word for word in words):
        pytest.skip("a template with a placeholder")
    monkeypatch.chdir(ROOT)
    # `list-platforms` looks in the default PDK store, which is under `HOME`.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    words = [PLACEHOLDER_DESIGNS.get(word, word) for word in words]
    ran_for_real = True
    if words[0] == "skill":
        monkeypatch.chdir(tmp_path)  # `skill install` writes into the working directory
    if words[0] in NOT_RUN:
        pytest.skip(NOT_RUN[words[0]])
    if words[0] == "run":
        flow = words[1]
        words += ["--run-root", str(tmp_path / "xeda_run")]
        if flow in REAL_SIMULATORS:
            REAL_SIMULATORS[flow]()
            design_file = next(word for word in words[2:] if word.endswith(".yaml"))
            if Design.from_file(ROOT / design_file).tb.cocotb:
                tool_utils.require_cocotb()
        elif "--dry-run" not in words:
            words.append("--dry-run")
            ran_for_real = False
    result = CliRunner().invoke(cli, words)
    assert result.exit_code == 0, f"{where}: xeda {' '.join(words)}\n{result.output}"
    jq = _jq_filter(text)
    if jq is None:
        return
    assert where not in UNCHECKED_PIPELINES, UNCHECKED_PIPELINES[where]
    paths = JQ_PATH.findall(jq)
    assert paths, f"{where}: no path in the jq filter {jq!r}: say why it is not checked"
    if ran_for_real:
        document = json.loads(result.stdout)
    else:
        # the run was planned, not made (it needs a vendor tool): its document has the shape of
        # a real run's, and the result keys it reads are documented for the flow
        document = request.getfixturevalue("run_document")
    for path in paths:
        if not ran_for_real and path.startswith(".results."):
            key = path.split(".")[2].split("[")[0]
            assert key in _results_keys(words[1]), f"{where}: {words[1]} documents no result {key}"
            path = ".results"
        assert (problem := _missing_in(document, path)) is None, f"{where}: {path}: {problem}"


def test_the_path_checker_tells_an_index_from_every_element():
    """`[0]` is the first element only, and `[]` is every element, as in `jq`."""
    document = {"items": [{}, {"wanted": True}]}
    assert _missing_in(document, ".items[1].wanted") is None
    assert _missing_in(document, ".items[0].wanted") == ".items[0].wanted is not in the document"
    assert _missing_in(document, ".items[].wanted") == ".items[].wanted is not in the document"
    assert _missing_in({"items": [{"wanted": 1}]}, ".items[0].wanted") is None
    assert _missing_in({"items": []}, ".items[0]") == ".items[0] is not in the document"
