"""A design file that cannot be loaded is reported as one error naming the file.

Every way a design file can fail to load -- unreadable, not TOML/JSON/YAML, a format xeda does
not read, not a table of design fields -- is a `DesignFileParseError` that names the file and,
when the parser knows it, the (1-based) line and column. A file that parses but does not
validate is a `DesignValidationError`, which names the file as well. Both are `XedaException`s,
so the CLI reports them the way it reports every user error: one CRITICAL line and exit status
1 in text mode, one JSON document under `--json`, never a traceback.

The same goes for the launcher's own user errors: naming a design the project does not have,
or giving no design at all, used to be a `ValueError` traceback (or a `None` the CLI reported as
a flow that "did not complete successfully").
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.design import Design, DesignFileParseError, DesignValidationError
from xeda.utils import XedaException

TESTS_DIR = Path(__file__).parent.absolute()
SQRT_DIR = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS_DIR = TESTS_DIR / "fake_tools"

#: name -> (file content, expected line, expected column); `None` where no position is known.
MALFORMED = {
    "bad.toml": ('name = "bad"\n[rtl\nsources = ["sqrt.vhdl"]\n', 2, 5),
    # one line: JSONDecodeError's lineno/colno are already 1-based, and adding 1 to them used to
    # report "line 2, column 26" for this file, which has no second line
    "bad.json": ('{"name": "bad", "rtl": {', 1, 25),
    # a problem with no enclosing context: only the problem mark says where it is
    "bad.yaml": ("a: b: c\n", 1, 5),
    "unterminated.yaml": ('name: bad\nrtl:\n  top: "a\n', 4, 1),
}


def write(path: Path, content) -> Path:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def run_xeda(*args: str, cwd: Path, fake_tools: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ, COLUMNS="200")
    if fake_tools:
        env["PATH"] = str(FAKE_TOOLS_DIR) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def assert_one_error_document(proc, error_type: str, *mentions: str) -> dict:
    """stdout is one JSON document reporting `error_type`, and the message names its kind once."""
    assert proc.returncode == 1, proc.stderr
    try:
        document = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout is not one JSON document ({e}):\n{proc.stdout}\n{proc.stderr}")
    assert document["success"] is False
    error = document["error"]
    assert error["type"] == error_type, error
    assert f"{error_type}: {error_type}" not in error["message"], error["message"]
    for mention in mentions:
        assert mention in error["message"], error["message"]
    assert "Traceback" not in proc.stderr, proc.stderr
    return document


def assert_one_critical_line(proc, *mentions: str) -> None:
    """Text mode: exit status 1, the error on a CRITICAL line, no traceback."""
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    critical = [line for line in proc.stderr.splitlines() if "CRITICAL" in line]
    assert len(critical) == 1, proc.stderr
    for mention in mentions:
        assert mention in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------------------------
# Design.from_file: every load failure is a DesignFileParseError naming the file
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_design_file_is_a_parse_error_at_its_position(tmp_path, name):
    content, line, column = MALFORMED[name]
    path = write(tmp_path / name, content)
    with pytest.raises(DesignFileParseError) as raised:
        Design.from_file(path)
    error = raised.value
    assert isinstance(error, XedaException)
    assert error.file == str(path.absolute())
    assert (error.line, error.column) == (line, column)
    assert str(path.absolute()) in str(error)
    assert f"line {line}, column {column}" in str(error)


@pytest.mark.parametrize(
    "name,content,mentions",
    [
        pytest.param("design.txt", "x\n", ["'.txt'", ".toml", ".json", ".yaml"], id="unsupported"),
        # suffixes are case-sensitive, like every other name xeda reads: a mis-cased one is
        # rejected naming the right spelling, not read as whatever it resembles
        pytest.param("Sqrt.TOML", 'name = "s"\n', ["'.TOML'", "did you mean '.toml'"], id="case"),
        pytest.param("design.Yml", "name: s\n", ["did you mean '.yml'"], id="case-yml"),
        pytest.param("list.yaml", "- a\n- b\n", ["list"], id="not-a-table"),
        pytest.param("scalar.json", '"just text"', ["str"], id="scalar"),
        pytest.param("latin1.toml", b'name = "caf\xe9"\n', ["UTF-8"], id="not-utf8"),
    ],
)
def test_a_file_that_is_not_a_design_is_a_parse_error_naming_it(tmp_path, name, content, mentions):
    path = write(tmp_path / name, content)
    with pytest.raises(DesignFileParseError) as raised:
        Design.from_file(path)
    assert raised.value.file == str(path.absolute())
    message = str(raised.value)
    assert str(path.absolute()) in message
    for mention in mentions:
        assert mention in message


def test_an_unreadable_design_file_is_a_parse_error_naming_it(tmp_path):
    with pytest.raises(DesignFileParseError, match=re.escape("missing.toml")):
        Design.from_file(tmp_path / "missing.toml")
    (tmp_path / "dir.toml").mkdir()
    with pytest.raises(DesignFileParseError, match=re.escape("dir.toml")):
        Design.from_file(tmp_path / "dir.toml")


def test_a_design_validation_error_names_the_file(tmp_path):
    """`from_file` attaches the file to the error, and the error has to say it: without it the
    CLI printed only "1 error validating design 'bad'", naming no file at all."""
    write(tmp_path / "sqrt.vhdl", (SQRT_DIR / "sqrt.vhdl").read_text())
    path = write(
        tmp_path / "badval.toml", 'name = "bad"\n[rtl]\nsources = ["sqrt.vhdl"]\ntop = 5\n'
    )
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(path)
    assert isinstance(raised.value, XedaException)
    assert raised.value.file == str(path.absolute())
    assert str(path.absolute()) in str(raised.value)
    # and the kind is named once
    assert str(raised.value).count("DesignValidationError") == 1


# ---------------------------------------------------------------------------------------------
# the CLI: one CRITICAL line in text mode, one JSON document under --json
# ---------------------------------------------------------------------------------------------

CLI_CASES = {
    "bad.toml": ('name = "bad"\n[rtl\n', "DesignFileParseError", "line 2, column 5"),
    "bad.json": ('{"name": "bad", "rtl": {', "DesignFileParseError", "line 1, column 25"),
    "Sqrt.TOML": ('name = "s"\n', "DesignFileParseError", "did you mean '.toml'"),
    "design.txt": ("x\n", "DesignFileParseError", "'.txt'"),
    "badval.toml": ('name = "bad"\n[rtl]\ntop = 5\n', "DesignValidationError", "rtl.top"),
}


@pytest.mark.parametrize("name", sorted(CLI_CASES))
@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_xeda_run_reports_an_unloadable_design_file_cleanly(tmp_path, name, json_flag):
    content, error_type, detail = CLI_CASES[name]
    path = write(tmp_path / name, content)
    args = ["run", "ghdl_sim", str(path)] + (["--json"] if json_flag else [])
    proc = run_xeda(*args, cwd=tmp_path)
    if json_flag:
        assert_one_error_document(proc, error_type, str(path), detail)
    else:
        assert_one_critical_line(proc, error_type, str(path), detail)


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_xeda_dse_reports_an_invalid_design_file_once(tmp_path, json_flag):
    """`dse` prefixed every message with its exception's type, and `DesignValidationError`
    already starts its message with it: "DesignValidationError: DesignValidationError: ..."."""
    path = write(tmp_path / "badval.toml", CLI_CASES["badval.toml"][0])
    args = ["dse", "vivado_synth", "--design", str(path), "--xeda-run-dir", str(tmp_path / "r")]
    args += ["--init-freq-low", "100", "--init-freq-high", "200"]
    proc = run_xeda(*args, *(["--json"] if json_flag else []), cwd=tmp_path)
    if json_flag:
        assert_one_error_document(proc, "DesignValidationError", str(path), "rtl.top")
    else:
        assert proc.returncode == 1
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "DesignValidationError: DesignValidationError" not in proc.stderr


PROJECT = """
[[design]]
name = "sqrt"
[design.rtl]
sources = ["{sqrt}"]
top = "sqrt"
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "xedaproject.toml"
    project.write_text(PROJECT.format(sqrt=SQRT_DIR / "sqrt.vhdl"))
    return project


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_a_design_name_the_project_lacks_is_reported_cleanly(tmp_path, json_flag):
    _project(tmp_path)
    args = ["run", "ghdl_sim", "--design-name", "nosuch"] + (["--json"] if json_flag else [])
    proc = run_xeda(*args, cwd=tmp_path)
    if json_flag:
        assert_one_error_document(proc, "DesignNotFoundError", "nosuch", "sqrt")
    else:
        assert_one_critical_line(proc, "DesignNotFoundError", "nosuch", "sqrt")


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_a_design_name_without_a_project_is_reported_as_such(tmp_path, json_flag):
    """No project to look the name up in: this is not a flow that "did not complete"."""
    args = ["run", "ghdl_sim", "--design-name", "nosuch"] + (["--json"] if json_flag else [])
    proc = run_xeda(*args, cwd=tmp_path)
    if json_flag:
        assert_one_error_document(proc, "DesignNotFoundError", "xedaproject.toml")
    else:
        assert_one_critical_line(proc, "DesignNotFoundError", "xedaproject.toml")


@pytest.mark.parametrize(
    "content,detail",
    [
        pytest.param("[[design]\n", "line 1", id="malformed"),
        pytest.param("flows = 3\n", "flows", id="invalid"),
    ],
)
def test_a_project_file_that_cannot_be_loaded_is_reported_cleanly(tmp_path, content, detail):
    (tmp_path / "xedaproject.toml").write_text(content)
    proc = run_xeda("run", "ghdl_sim", "--design-name", "sqrt", "--json", cwd=tmp_path)
    assert_one_error_document(proc, "ProjectFileError", "xedaproject.toml", detail)


def test_the_runner_raises_xeda_exceptions_for_user_errors(tmp_path):
    """Library callers get the same typed errors the CLI reports."""
    from xeda.flow_runner import DefaultRunner
    from xeda.flow_runner.default_runner import DesignNotFoundError

    project = _project(tmp_path)
    runner = DefaultRunner(tmp_path / "run")
    with pytest.raises(DesignNotFoundError, match="nosuch") as raised:
        runner.run("ghdl_sim", "nosuch", xedaproject=str(project))
    assert isinstance(raised.value, XedaException)
    # a string with a design-file suffix is a design file, whatever its case -- and the loader
    # then names the right spelling rather than looking it up as a design name
    write(tmp_path / "Sqrt.TOML", 'name = "s"\n')
    old = Path.cwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(DesignFileParseError, match=re.escape("did you mean '.toml'")):
            runner.run("ghdl_sim", "Sqrt.TOML")
        with pytest.raises(DesignFileParseError, match=re.escape("missing.toml")):
            runner.run("ghdl_sim", "missing.toml")
    finally:
        os.chdir(old)
