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

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"
FAKE_TOOLS_DIR = TESTS_DIR / "fake_tools"
SQRT = EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml"


def run_xeda(*args: str, cwd=None, fake_tools: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ, COLUMNS="80")
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


def test_missing_design_reports_json_and_fails():
    proc = run_xeda("run", "vivado_synth", "--json")
    document = json_stdout(proc)
    assert proc.returncode != 0
    assert document["success"] is False
    assert document["error"]["type"] == "DesignNotSpecified"


def test_unknown_flow_reports_json_and_fails():
    proc = run_xeda("run", "no_such_flow", str(SQRT), "--json")
    document = json_stdout(proc)
    assert proc.returncode != 0
    assert document["success"] is False
    assert "no_such_flow" in document["error"]["message"]


def test_unknown_option_reports_json_and_fails():
    """Click handles UsageError itself; without interception stdout would be empty."""
    proc = run_xeda("run", "vivado_synth", str(SQRT), "--no-such-option", "--json")
    document = json_stdout(proc)
    assert proc.returncode != 0
    assert document["error"]["type"] == "NoSuchOption"


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


# --------------------------------------------------------------- flow-name resolution at the CLI


@pytest.mark.parametrize(
    "name,canonical",
    [
        ("vivado_synth", "vivado_synth"),
        ("vivado-synth", "vivado_synth"),
        ("VivadoSynth", "vivado_synth"),  # the CamelCase class name
        ("ghdl", "ghdl_sim"),  # an alias
        ("OpenXC7", "open_xc7"),  # lossy snake_case round-trip
    ],
)
def test_cli_accepts_every_name_the_resolver_accepts(name, canonical):
    proc = run_xeda("list-settings", name, "--json")
    assert proc.returncode == 0, proc.stderr
    assert json_stdout(proc)["flow"] == canonical


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
        timeout=300,
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
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"success": True}
    assert "RAW GENERATOR OUTPUT" in proc.stderr


REMOTE_SCRIPT = """
import sys
from xeda.flow_runner import remote

class FakeRunner:
    def __init__(self, *a, **k): pass
    def run_remote(self, *a, **k):
        return {"success": %s, "run_path": "/tmp/remote-run"}

remote.RemoteRunner = FakeRunner
from xeda.cli import cli
cli(["run", "vivado_synth", %r, "--remote", "host", "--json"], standalone_mode=True)
"""


@pytest.mark.parametrize("remote_success,expected_exit", [(True, 0), (False, 1)])
def test_remote_status_comes_from_the_remote_results(remote_success, expected_exit, tmp_path):
    """A failed remote flow used to be reported as a successful run."""
    proc = subprocess.run(
        [sys.executable, "-c", REMOTE_SCRIPT % (remote_success, str(SQRT))],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    document = json_stdout(proc)
    assert proc.returncode == expected_exit
    assert document["success"] is remote_success
    assert document["run_path"] == "/tmp/remote-run"
    assert document["results"]["success"] is remote_success
    if not remote_success:
        assert document["error"]["type"] == "FlowFailed"


def test_remote_output_is_wired_to_the_redirected_stream():
    """The remote flow's stdout is tool output and must follow the same redirection.

    `run_remote` needs a live SSH connection, so this checks the wiring at the source: it used
    to hand `RemoteLogger` `sys.stdout` unconditionally.
    """
    import inspect

    from xeda.flow_runner.remote import RemoteLogger, RemoteRunner

    source = inspect.getsource(RemoteRunner.run_remote)
    stdout_wiring = [
        line for line in source.splitlines() if "RemoteLogger" in line or 'label="stdout"' in line
    ]
    assert any("tool_output_stream()" in line for line in stdout_wiring), stdout_wiring
    assert not any("sys.stdout" in line for line in stdout_wiring), stdout_wiring

    # and RemoteLogger writes to whatever stream it is given
    import io

    stream = io.StringIO()
    RemoteLogger(stream, label="stdout").cb("hello")
    assert stream.getvalue() == "hello"


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
    assert document["design"] == "gendes"
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
    assert document["design"] == "gendes"
    assert "generator stdout noise" in proc.stderr
    assert "generator stderr diagnostic" in proc.stderr
