"""The machine-readable CLI surface that coding agents and scripts depend on.

Two guarantees are under test:

* query commands (`list-flows`, `list-settings`, ...) can emit JSON/JSONL/YAML that is never
  truncated to the terminal width, and
* `run --json` puts *only* the JSON document on stdout -- tool output, logs and the results
  table go to stderr -- so its stdout can be piped straight into a parser.

Everything runs as a subprocess: stdout/stderr separation is the property being verified, and
the machine-readable mode mutates process-global state (the rich console, the tool output
stream) that must not leak into other tests.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"
FAKE_TOOLS_DIR = TESTS_DIR / "fake_tools"
SQRT_DESIGN = EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml"

QUERY_COMMANDS = [
    ["list-flows"],
    ["list-settings", "vivado_synth"],
    ["list-results", "vivado_synth"],
    ["list-boards"],
    ["list-platforms"],
    ["list-optimizers"],
]


def run_xeda(
    *args: str, cwd: Optional[Path] = None, fake_tools: bool = False, check: bool = False
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if fake_tools:
        env["PATH"] = str(FAKE_TOOLS_DIR) + os.pathsep + env.get("PATH", "")
    # A narrow terminal is the case that used to truncate identifiers in the table output.
    env["COLUMNS"] = "80"
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=check,
    )


@pytest.mark.parametrize("command", QUERY_COMMANDS, ids=lambda c: "-".join(c))
def test_query_command_json_is_parseable(command: List[str]):
    proc = run_xeda(*command, "--json")
    assert proc.returncode == 0, proc.stderr
    json.loads(proc.stdout)


@pytest.mark.parametrize("command", QUERY_COMMANDS, ids=lambda c: "-".join(c))
def test_json_flag_is_shorthand_for_format_json(command: List[str]):
    assert run_xeda(*command, "--json").stdout == run_xeda(*command, "--format", "json").stdout


@pytest.mark.parametrize("command", QUERY_COMMANDS, ids=lambda c: "-".join(c))
def test_query_command_jsonl_is_one_object_per_line(command: List[str]):
    proc = run_xeda(*command, "--format", "jsonl")
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    for line in lines:
        assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize("command", QUERY_COMMANDS, ids=lambda c: "-".join(c))
def test_query_command_yaml_is_parseable(command: List[str]):
    proc = run_xeda(*command, "--format", "yaml")
    assert proc.returncode == 0, proc.stderr
    yaml.safe_load(proc.stdout)


def test_list_flows_json_reports_canonical_names_and_aliases():
    flows = json.loads(run_xeda("list-flows", "--json").stdout)
    by_name = {f["name"]: f for f in flows}
    for required in ("name", "aliases", "class", "category", "dependencies", "description"):
        assert all(required in f for f in flows)
    # aliases are folded into their flow rather than listed as separate flows
    assert "ghdl" not in by_name
    assert "ghdl" in by_name["ghdl_sim"]["aliases"]
    # statically-detected dependencies
    assert by_name["vivado_postsynth_sim"]["dependencies"] == ["vivado_synth"]


def test_list_settings_json_never_truncates_identifiers():
    """The table used to render `set_synth_proper…`, which cannot be typed back into -s KEY=VALUE."""
    info = json.loads(run_xeda("list-settings", "vivado_synth", "--json").stdout)
    names = {f["name"] for f in info["fields"]}
    assert "set_synth_properties" in names
    assert "fail_critical_warning" in names
    assert not any("…" in n for n in names)


def test_list_settings_includes_common_settings_and_aliases():
    info = json.loads(run_xeda("list-settings", "vivado_synth", "--json").stdout)
    by_name = {f["name"]: f for f in info["fields"]}
    # settings shared by every flow used to be hidden entirely
    assert by_name["nthreads"]["common"] is True
    assert by_name["nthreads"]["alias"] == "ncpus"
    assert by_name["dockerized"]["common"] is True
    assert by_name["clock_period"]["common"] is False


def test_list_settings_preserves_optional_literal_choices():
    info = json.loads(run_xeda("list-settings", "ghdl_sim", "--json").stdout)
    by_name = {f["name"]: f for f in info["fields"]}
    assert by_name["asserts"]["enum"] == ["disable", "disable-at-0"]
    assert by_name["asserts"]["required"] is False


@pytest.mark.parametrize("flow", ["yosys", "yosys_fpga"])
def test_list_settings_preserves_single_value_literal_choices(flow):
    """pydantic 2 emits a one-value `Literal` as `const` rather than `enum`."""
    info = json.loads(run_xeda("list-settings", flow, "--json").stdout)
    by_name = {f["name"]: f for f in info["fields"]}
    assert by_name["stop_after"]["enum"] == ["rtl"]
    assert by_name["stop_after"]["required"] is False


def test_list_settings_no_common_excludes_shared_settings():
    info = json.loads(run_xeda("list-settings", "vivado_synth", "--json", "--no-common").stdout)
    assert all(not f["common"] for f in info["fields"])
    assert {f["name"] for f in info["fields"]}


def test_list_results_reports_common_and_flow_specific_keys():
    info = json.loads(run_xeda("list-results", "vivado_synth", "--json").stdout)
    names = {k["name"] for k in info["keys"]}
    assert {"success", "runtime", "run_path"} <= names
    assert "Fmax" in names


def test_list_results_of_a_cocotb_sim_flow_documents_cocotb_keys():
    info = json.loads(run_xeda("list-results", "nvc", "--json").stdout)
    documented = {k["name"]: k for k in info["keys"]}
    assert documented["cocotb.tests"]["description"]


def test_design_schema_is_a_usable_json_schema():
    schema = json.loads(run_xeda("design-schema").stdout)
    assert "name" in schema["properties"]
    sources = schema["$defs"]["RtlSettings"]["properties"]["sources"]
    assert sources["items"]["anyOf"][0]["type"] == "string"


def test_table_output_does_not_truncate_setting_names():
    """At 80 columns the name must wrap, not be replaced by an ellipsis."""
    proc = run_xeda("list-settings", "vivado_synth")
    assert proc.returncode == 0, proc.stderr
    assert "…" not in proc.stdout


def test_run_json_stdout_carries_only_the_json_document(tmp_path):
    proc = run_xeda(
        "run",
        "vivado_synth",
        str(SQRT_DESIGN),
        "-s",
        "clock.period=5.5",
        "--json",
        cwd=tmp_path,
        fake_tools=True,
    )
    document = json.loads(proc.stdout)  # would raise if any tool output leaked to stdout
    assert document["success"] is True, proc.stderr
    assert proc.returncode == 0
    assert document["flow"] == "vivado_synth"
    assert document["design"] == "sqrt"
    assert Path(document["run_path"]).is_dir()
    assert Path(document["results_json"]).is_file()
    assert document["results"]["success"] is True
    # the tool actually ran, and its output went to stderr
    assert "Running `vivado" in proc.stderr


def test_run_design_file_option_selects_the_design(tmp_path):
    proc = run_xeda(
        "run",
        "vivado_synth",
        "--design-file",
        str(SQRT_DESIGN),
        "-s",
        "clock.period=5.5",
        "--json",
        cwd=tmp_path,
        fake_tools=True,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr
    assert document["success"] is True
    assert document["design"] == "sqrt"


def test_run_json_reports_failures_as_json_and_a_nonzero_exit(tmp_path):
    proc = run_xeda(
        "run",
        "vivado_synth",
        str(SQRT_DESIGN),
        "-s",
        "no_such_setting=1",
        "--json",
        cwd=tmp_path,
        fake_tools=True,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode != 0
    assert document["success"] is False
    assert document["error"]["type"] == "FlowSettingsError"
    assert "no_such_setting" in document["error"]["message"]


def test_failed_run_document_includes_an_error():
    from xeda.cli import _run_document

    document = _run_document("vivado_synth", "sqrt.toml", None, False)
    assert document["success"] is False
    assert document["error"]["type"] == "FlowFailed"


def test_unknown_flow_suggests_a_close_match():
    proc = run_xeda("list-settings", "vivado_synt")
    assert proc.returncode != 0
    # click rejects it against the choice list, which itself names the valid flows
    assert "vivado_synth" in (proc.stderr + proc.stdout)


def test_help_does_not_require_a_terminal():
    for args in (["--help"], ["run", "--help"], ["list-settings", "--help"]):
        proc = run_xeda(*args)
        assert proc.returncode == 0, proc.stderr
        assert "Usage:" in proc.stdout


def test_no_incremental_help_says_what_happens_to_the_previous_run(tmp_path):
    """`--no-incremental` deletes the previous run directory: the CLI never asks the launcher
    for backups. Its help used to say it "backs up or removes" it -- on a destructive option,
    the one word that matters."""
    import click

    help_text = " ".join(click.unstyle(run_xeda("run", "--help", check=True).stdout).split())
    assert "backs up" not in help_text
    assert "--no-incremental deletes" in help_text

    args = ("run", "vivado_synth", str(SQRT_DESIGN), "-s", "clock.period=5.5", "--json")
    first = json.loads(run_xeda(*args, cwd=tmp_path, fake_tools=True).stdout)
    run_path = Path(first["run_path"])
    (run_path / "MARKER").write_text("from the previous run\n")
    second = json.loads(run_xeda(*args, "--no-incremental", cwd=tmp_path, fake_tools=True).stdout)
    assert second["success"] is True
    assert Path(second["run_path"]) == run_path
    assert not (run_path / "MARKER").exists()
    assert sorted(p.name for p in run_path.parent.iterdir()) == [run_path.name]  # no backup
