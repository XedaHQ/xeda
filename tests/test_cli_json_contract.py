"""The `--json` contract: a parseable document on stdout, always, and a truthful exit status.

Everything here runs as a subprocess, because the contract is about stdout/stderr separation and
because machine-readable mode mutates process-global state (the rich console, the tool output
stream) that must not leak into other tests.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.platforms import bundled_platform_names

from .remote_fixtures import fake_remote, fake_ssh, remote_host
from .test_remote_client import _cli_script, _design
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.python_compat

FIXTURES = (fake_ssh, fake_remote, remote_host)


TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"
FAKE_TOOLS_DIR = TESTS_DIR / "fake_tools"
SQRT = EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml"


def run_xeda(
    *args: str, cwd=None, fake_tools: bool = False, home=None
) -> subprocess.CompletedProcess:
    env = dict(os.environ, COLUMNS="80")
    if home is not None:
        env["HOME"] = str(home)  # the default PDK store is under HOME
    if fake_tools:
        env["PATH"] = str(FAKE_TOOLS_DIR) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def json_stdout(proc: subprocess.CompletedProcess):
    """Parse stdout, failing with the captured streams if anything else leaked into it."""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout was not a single JSON document ({e})\nstdout:\n{proc.stdout}")


# --------------------------------------------------------------- errors always produce a document


def unstarted(error: dict, spec=None) -> dict:
    """The document of a launch that did not start, with the whole shape written out."""
    return {
        "schema_version": 1,
        "outcome": None,
        "error": {"node": None, "exit_status": None, "log": None, **error},
        "launch_id": None,
        "spec": spec,
        "roots": [],
        "nodes": [],
    }


def test_missing_design_reports_json_and_fails():
    proc = run_xeda("run", "vivado_synth", "--json")
    document = json_stdout(proc)
    assert proc.returncode == 2
    assert document["outcome"] is None and document["spec"] is None
    assert document["error"]["code"] == "usage_error"
    assert "No design specified" in document["error"]["message"]


def test_unknown_flow_reports_json_and_fails():
    proc = run_xeda("run", "no_such_flow", str(SQRT), "--json")
    document = json_stdout(proc)
    assert proc.returncode == 2
    assert document["outcome"] is None and document["error"]["code"] == "usage_error"
    assert "no_such_flow" in document["error"]["message"]


def test_an_unknown_option_prints_the_invalid_launch_document():
    """Click handles UsageError itself; without interception stdout would be empty. The command
    line is no spec, so the document has none."""
    proc = run_xeda("run", "vivado_synth", str(SQRT), "--no-such-option", "--json")
    document = json_stdout(proc)
    assert proc.returncode == 2
    assert document == unstarted(
        {"code": "usage_error", "message": "No such option '--no-such-option'."}
    )
    assert list(document) == [
        "schema_version",
        "outcome",
        "error",
        "launch_id",
        "spec",
        "roots",
        "nodes",
    ]


def test_an_invalid_setting_exits_2_with_a_null_outcome(tmp_path):
    """A misspelled setting of a flow starts no launch. The valid spelling dry-runs."""
    proc = run_xeda("run", "nextpnr", str(SQRT), "-s", "sed=1", "--json", cwd=tmp_path)
    document = json_stdout(proc)
    assert proc.returncode == 2
    assert document["outcome"] is None and document["launch_id"] is None
    assert (document["roots"], document["nodes"]) == ([], [])
    assert document["error"]["code"] == "unknown_setting"
    assert document["error"]["node"] == "nextpnr" and "sed" in document["error"]["message"]
    assert document["spec"] == {
        "flow": "nextpnr",
        "design": "sqrt",
        "design_file": str(SQRT),
        "target": None,
        "settings": {"nextpnr": {"sed": "1"}},
    }
    valid = run_xeda(
        "run", "nextpnr", str(SQRT), "-s", "seed=1", "--dry-run", "--json", cwd=tmp_path
    )
    assert valid.returncode == 0, valid.stderr
    assert json_stdout(valid)["success"] is True


def test_the_public_error_of_an_invalid_setting_names_the_setting_and_the_right_spelling(tmp_path):
    from xeda.flow_runner import DefaultRunner
    from xeda.flow_runner.error_codes import public_error

    with pytest.raises(Exception) as refused:
        DefaultRunner(tmp_path / "run").resolve_run("nextpnr", str(SQRT), flow_settings=["sed=1"])
    error = public_error(refused.value)
    assert (error.code, error.node, error.setting) == ("unknown_setting", "nextpnr", "sed")
    assert error.suggestions == ("seed",)


def test_dse_unknown_optimizer_reports_json_and_fails():
    proc = run_xeda("dse", "vivado_synth", "--design", str(SQRT), "--optimizer", "nope", "--json")
    document = json_stdout(proc)
    assert proc.returncode != 0
    assert document["success"] is False
    assert document["error"]["type"] == "OptimizerNotFound"
    # the message must name the alternatives rather than just rejecting
    assert "fmax_optimizer" in document["error"]["message"]


@pytest.mark.parametrize("command", [["run"], ["list-flows"], ["dse"]])
def test_help_still_works_alongside_json(command):
    """Intercepting usage errors must not swallow --help."""
    proc = run_xeda(*command, "--help", "--json")
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout


def test_group_level_json_is_itself_a_usage_error():
    """`--json` belongs to the subcommands, so the group rejecting it is correct -- and the
    rejection is still reported as JSON."""
    proc = run_xeda("--json", "--help")
    assert proc.returncode != 0
    assert json_stdout(proc)["error"]["type"] == "NoSuchOption"


# ------------------------------------------------------------- the PDK commands read one store


@pytest.mark.parametrize("command", [["list-platforms"], ["pdk", "list"]])
def test_the_pdk_queries_put_one_document_on_stdout(command, tmp_path):
    proc = run_xeda(*command, "--json", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert {row["platform"] for row in json_stdout(proc)} == set(bundled_platform_names())


@pytest.mark.parametrize(
    ("command", "error_type"),
    [
        (["pdk", "path", "nangate45"], "PdkNotInstalled"),  # the empty store under HOME
        (["pdk", "path", "nangate"], "BadParameter"),
        (["pdk", "path"], "UsageError"),
        (["pdk", "path", "nangate45", "--no-such-option"], "NoSuchOption"),
        # An install that fails before it writes or uses the network.
        (["pdk", "install", "nangate45", "--provider", "directory"], "UsageError"),
        (["pdk", "install", "nangate45", "--version", "main"], "UsageError"),
        (["pdk", "install", "nangate"], "BadParameter"),
        (["pdk", "install"], "UsageError"),
        (["pdk", "path", "--platform-file", "MERGE_FAILS"], "BadParameter"),
    ],
    ids=lambda value: value if isinstance(value, str) else " ".join(value),
)
def test_a_failed_pdk_command_reports_json_and_fails(command, error_type, tmp_path):
    if "MERGE_FAILS" in command:  # a file that loads alone but is wrong over nangate45
        broken = tmp_path / "mine.yaml"
        broken.write_text("name: mine\nextends: nangate45\ncorners: {default: missing}\n")
        command = [str(broken) if word == "MERGE_FAILS" else word for word in command]
    proc = run_xeda(*command, "--json", home=tmp_path)
    document = json_stdout(proc)
    assert proc.returncode != 0
    assert document["success"] is False
    assert document["error"]["type"] == error_type


# --------------------------------------------------------------- flow-name resolution at the CLI


@pytest.mark.parametrize(
    "name,canonical",
    [
        ("vivado_synth", "vivado_synth"),
        ("vivado-synth", "vivado_synth"),
        ("VivadoSynth", "vivado_synth"),  # the CamelCase class name
        ("ghdl", "ghdl_sim"),  # an alias
        ("FpgaPack", "fpga_pack"),
        ("fpga-pack", "fpga_pack"),
        ("YosysFpga", "yosys_fpga"),
    ],
)
def test_cli_accepts_every_name_the_resolver_accepts(name, canonical):
    proc = run_xeda("list-settings", name, "--json")
    assert proc.returncode == 0, proc.stderr
    assert json_stdout(proc)["flow"] == canonical


@pytest.mark.parametrize("name", ["open_xc7", "openxc7", "OpenXC7", "open-xc7"])
def test_the_removed_open_xc7_is_refused_by_name_with_its_replacements(name):
    """The CLI and the API share one diagnostic (`get_flow_class`)."""
    message = "`open_xc7` was removed: use fpga_pack to build, openfpgaloader to program"
    proc = run_xeda("list-settings", name, "--json")
    assert proc.returncode != 0
    assert message in json_stdout(proc)["error"]["message"]
    proc = run_xeda("run", name, "--json")
    assert proc.returncode != 0
    assert message in json_stdout(proc)["error"]["message"]
    listed = json_stdout(run_xeda("list-flows", "--json"))
    names = [name for flow in listed for name in (flow["name"], *flow.get("aliases", []))]
    assert "fpga_pack" in names and not [name for name in names if "xc7" in name.lower()]


def test_unknown_flow_name_suggests_close_matches():
    proc = run_xeda("list-settings", "vivado_synt", "--json")
    assert proc.returncode != 0
    assert "vivado_synth" in json_stdout(proc)["error"]["message"]


# --------------------------------------------------------------- nothing else may reach stdout

GENERATOR_SCRIPT = """
import json, sys
from xeda import proc_utils
from xeda.design import Generator

proc_utils.set_tool_output(sys.stderr)          # what `--json` mode does
gen = Generator(executable=sys.executable)
gen.run_cmd([sys.executable, "-c", "print('GENERATOR OUTPUT')"])
json.dump({"success": True}, sys.stdout)
"""


def test_generator_subprocess_output_does_not_corrupt_stdout(tmp_path):
    """A generator spawns subprocesses directly, bypassing run_process."""
    proc = subprocess.run(
        [sys.executable, "-c", GENERATOR_SCRIPT],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"success": True}
    assert "GENERATOR OUTPUT" in proc.stderr


RAW_GENERATOR_SCRIPT = """
import json, sys
from xeda import proc_utils
from xeda.design import Design

proc_utils.set_tool_output(sys.stderr)
Design.process_generation({
    "design_root": ".",
    "rtl": {
        "generator": [sys.executable, "-c", "print('RAW GENERATOR OUTPUT')"],
    },
})
json.dump({"success": True}, sys.stdout)
"""


def test_raw_generator_command_output_does_not_corrupt_stdout(tmp_path):
    """The list form in a design file bypasses ``Generator.run_cmd``."""
    proc = subprocess.run(
        [sys.executable, "-c", RAW_GENERATOR_SCRIPT],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"success": True}
    assert "RAW GENERATOR OUTPUT" in proc.stderr


REMOTE_SCRIPT = """
import sys
from xeda.remote import client

def fake_run_remote(chain, **arguments):
    assert chain == "vivado_synth" and arguments["destination_text"] == "host"
    assert arguments["target"] is None and arguments["xedaproject"] is None
    assert arguments["design_overrides"] == () and arguments["design_allow_extra"] is False
    assert arguments["fetch_all"] is False and arguments["dry_run"] is False
    passed = %(passed)s
    document = {
        "schema_version": 1, "outcome": "passed" if passed else "failed",
        "error": None if passed else {"code": "remote_failed", "message": "did not complete",
                                       "node": None, "exit_status": None, "log": None},
        "launch_id": "abc", "roots": [], "nodes": [],
        "spec": {"flow": "vivado_synth", "design": "sqrt", "design_file": %(design)r,
                 "target": None, "settings": {}},
    }
    return client.RemoteOutcome(document, passed)

client.run_remote = fake_run_remote
from xeda.cli import cli
cli(["run", "vivado_synth", %(design)r, "--remote", "host", "--json"], standalone_mode=True)
"""


@pytest.mark.parametrize("remote_passed,expected_exit", [(True, 0), (False, 1)])
def test_remote_status_comes_from_the_remote_outcome(remote_passed, expected_exit, tmp_path):
    """A failed remote flow is reported as a failed run, with the remote's own status."""
    script = REMOTE_SCRIPT % dict(design=str(SQRT), passed=remote_passed)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    document = json_stdout(proc)
    assert proc.returncode == expected_exit
    assert document["outcome"] == ("passed" if remote_passed else "failed")
    assert document["spec"]["design"] == "sqrt" and document["spec"]["design_file"] == str(SQRT)
    assert (document["error"] is None) is remote_passed
    if not remote_passed:
        assert document["error"]["code"] == "remote_failed"


def test_a_remote_run_puts_only_the_json_document_on_stdout(tmp_path, remote_host):
    """The real client and the real remote agent run: the log of the launch, the progress of the
    provisioning and the results table go to stderr, and nothing precedes or follows the
    document on stdout."""
    design = _design(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", _cli_script(tmp_path, design, remote_host, signals=False)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert proc.returncode == 0, proc.stderr
    document = json_stdout(proc)
    assert document["outcome"] == "passed" and document["error"] is None
    assert list(document) == [
        "schema_version",
        "outcome",
        "error",
        "launch_id",
        "spec",
        "roots",
        "nodes",
    ]
    assert "nextpnr-ecp5" in proc.stderr, "the log of the launch is tool output"


# ------------------------------------------------------- design generators must not reach stdout

#: A generator that writes to *both* streams. Only its stdout can corrupt the JSON document:
#: `stderr=None` makes a child inherit fd 2, which in machine-readable mode is exactly where tool
#: output belongs. Redirecting stdout without redirecting stderr is therefore correct, and a
#: well-meaning change that redirects stderr *instead* silently reintroduces the corruption.
_NOISY_GENERATOR = (
    "import sys\n"
    "sys.stdout.write('generator stdout noise\\n')\n"
    "sys.stderr.write('generator stderr diagnostic\\n')\n"
    "open('gen_out.vhdl', 'w').write('entity g is end entity;\\n')\n"
)


def _noisy_generator_design(tmp_path: Path, generator: str) -> Path:
    (tmp_path / "gen.py").write_text(_NOISY_GENERATOR)
    design = tmp_path / "gendes.toml"
    design.write_text(
        'name = "gendes"\n\n[rtl]\ntop = "g"\nsources = ["gen_out.vhdl"]\n'
        f"generator = {generator}\n"
    )
    return design


@pytest.mark.parametrize(
    "generator",
    [
        pytest.param('"{python} gen.py"', id="shell-string"),
        pytest.param('["{python}", "gen.py"]', id="argument-list"),
    ],
)
def test_generator_output_never_corrupts_the_json_document(tmp_path, generator):
    design = _noisy_generator_design(tmp_path, generator.format(python=sys.executable))
    proc = run_xeda("run", "ghdl_sim", design.name, "--json", cwd=tmp_path)
    document = json_stdout(proc)  # fails loudly if anything leaked into stdout
    assert document["spec"]["design"] == "gendes"
    # both streams of the generator belong on stderr, where tool output goes in --json mode
    assert "generator stdout noise" in proc.stderr
    assert "generator stderr diagnostic" in proc.stderr


def test_generator_object_output_never_corrupts_the_json_document(tmp_path):
    """The `Generator.run_cmd` path, which builds its own subprocess call."""
    (tmp_path / "gen.py").write_text(_NOISY_GENERATOR)
    design = tmp_path / "gendes.toml"
    design.write_text(
        'name = "gendes"\n\n[rtl]\ntop = "g"\nsources = ["gen_out.vhdl"]\n\n'
        f'[rtl.generator]\nexecutable = "{sys.executable}"\nargs = ["gen.py"]\n'
    )
    proc = run_xeda("run", "ghdl_sim", design.name, "--json", cwd=tmp_path)
    document = json_stdout(proc)
    assert document["spec"]["design"] == "gendes"
    assert "generator stdout noise" in proc.stderr
    assert "generator stderr diagnostic" in proc.stderr


# ------------------------------------------------- highlighted tool output must not reach stdout

#: `run_process` takes a separate branch when a tool has `highlight_rules` (Vivado, DC, VCS): it
#: pipes the child's stdout so it can colorize each line, and leaves the child's stderr alone.
#: Leaving it alone is correct -- an unspecified `stderr` makes the child inherit fd 2, and in
#: machine-readable mode fd 2 is exactly where tool output belongs. Only fd 1 carries the document.
_HIGHLIGHT_HARNESS = """
import sys, colorama
from xeda import proc_utils

proc_utils.set_tool_output(sys.stderr)          # what machine_readable_mode() does
sys.stdout.write('{"document": "begin", ')
sys.stdout.flush()
proc_utils.run_process(
    sys.executable,
    ["-c", "import sys;"
           "sys.stdout.write('TOOL-STDOUT: ERROR: highlighted\\\\n');"
           "sys.stderr.write('TOOL-STDERR: diagnostic\\\\n')"],
    highlight_rules={r"^(.*ERROR.*)$": colorama.Fore.RED + r"\\g<1>"},
    check=False,
)
sys.stdout.write('"document": "end"}')
"""


def test_highlighted_tool_output_does_not_corrupt_stdout(tmp_path):
    harness = tmp_path / "harness.py"
    harness.write_text(_HIGHLIGHT_HARNESS)
    proc = subprocess.run(
        [sys.executable, str(harness)],
        capture_output=True,
        text=True,
        env=dict(os.environ, FORCE_COLOR="1"),
        timeout=HANG_GUARD,
    )
    assert proc.returncode == 0, proc.stderr
    # stdout carries the document and nothing else
    assert json.loads(proc.stdout) == {"document": "end"}
    # both of the tool's streams land on stderr, and the highlight branch really ran
    assert "TOOL-STDOUT: ERROR: highlighted" in proc.stderr
    assert "TOOL-STDERR: diagnostic" in proc.stderr
    assert "\033[" in proc.stderr, "highlighting was not applied, so the branch was not exercised"
