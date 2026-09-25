"""`xeda run --remote`, end to end, without an SSH server.

Everything `RemoteRunner.run_remote` does runs for real except the transport: the design archive
is built by `send_design`, shipped, unpacked and run by the very `remote_runner` function the
remote host executes, in a separate Python process reached through an execnet gateway; tool
output streams back through `STREAM_OUTPUT_SETUP`; results, settings and artifacts come back and
are rewritten to local paths. Only SSH itself is replaced: fabric's `Connection` by one that
copies files on the local filesystem, and the `ssh=` gateway spec by execnet's `popen` gateway,
which bootstraps the same way (see `test_remote_streaming.py`). The "remote" home directory is a
separate tree, so nothing the remote run needs can be found by accident on the local side.
"""

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import execnet
import pytest

from xeda import Design
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.remote import RemoteRunner

from .tool_utils import require_ghdl

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


class _LocalSftp:
    def mkdir(self, path):
        os.mkdir(path)

    def chdir(self, path):
        assert os.path.isdir(path), path

    def open(self, path, mode="r"):
        return open(path, mode)


class _LocalConnection:
    """fabric's `Connection`, for a "remote" that is a directory on this machine."""

    def __init__(self, host, user=None, port=None):
        self.host = host

    def put(self, local, remote):
        shutil.copy(local, remote)

    def get(self, remote, local):
        shutil.copy(remote, local)
        return SimpleNamespace(local=local)

    def sftp(self):
        return _LocalSftp()

    def close(self):
        pass


class _LocalTransfer:
    def __init__(self, conn):
        pass

    def is_remote_dir(self, path):
        return os.path.isdir(path)


@pytest.fixture
def remote_host(tmp_path, monkeypatch):
    """A "remote" whose home is `tmp_path/remote_home`, running this interpreter (so this xeda)
    in a process of its own, with `tests/fake_tools` on its PATH."""
    home = tmp_path / "remote_home"
    home.mkdir()
    path = str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"]
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    monkeypatch.setattr(remote_module, "Transfer", _LocalTransfer)
    monkeypatch.setattr(
        remote_module, "get_login_env", lambda conn: {"PATH": path, "HOME": str(home)}
    )
    real_makegateway = execnet.makegateway

    def makegateway(spec):
        options = dict(part.split("=", 1) for part in spec.split("//"))
        assert "ssh" in options, spec  # what `run_remote` asked for
        return real_makegateway(
            f"popen//python={sys.executable}//chdir={options['chdir']}"
            f"//env:PATH={options['env:PATH']}"
        )

    monkeypatch.setattr(remote_module.execnet, "makegateway", makegateway)
    # xeda is started in a directory of its own, and the local run directory is elsewhere
    started_in = tmp_path / "started_in"
    started_in.mkdir()
    monkeypatch.chdir(started_in)
    return home


def _remote_run_dir(home: Path, flow_name: str) -> Path:
    """Locate the run directory created on the remote host."""
    (settings,) = home.glob(f".xeda/remote_run/*/*/{flow_name}*/settings.json")
    return settings.parent


#: What xeda 0.4.0, the oldest release a remote may run, accepts in a design's `rtl` and `tb`.
#: Every test above runs the "remote" on this very xeda, so none of them can see version skew;
#: but the archive is read by whatever xeda the remote host has, and that one forbids extra keys.
#: A key outside these fails *every* remote run on that release ("Extra inputs are not
#: permitted"); adding one means raising `REMOTE_XEDA_MIN_VERSION` on purpose.
RELEASED_RTL_KEYS = {
    "attributes",
    "clocks",
    "defines",
    "generator",
    "generics",
    "parameters",
    "sources",
    "top",
}
RELEASED_TB_KEYS = {"cocotb", "defines", "generics", "parameters", "sources", "top", "uut"}

EXAMPLE_DESIGNS = sorted(
    p
    for p in (TESTS_DIR.parent / "examples").rglob("*")
    if p.suffix in (".toml", ".yaml", ".yml")
    and p.name != "xedaproject.toml"
    and "xeda_run" not in p.parts
)


@pytest.mark.parametrize("design_file", EXAMPLE_DESIGNS, ids=lambda p: p.name)
def test_the_design_archive_is_readable_by_a_released_remote(design_file, tmp_path, monkeypatch):
    """The design archive is readable by a released remote."""
    design = Design.from_file(design_file)
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        shipped = json.loads(archive.read(design_name))

    assert set(shipped["rtl"]) <= RELEASED_RTL_KEYS
    assert set(shipped["tb"]) <= RELEASED_TB_KEYS


def test_the_archive_keeps_the_layout_an_include_is_resolved_by(tmp_path, monkeypatch):
    """A Verilog `include` finds the header beside the including file first, so a remote must
    see the sources laid out as they are here. Flattened into one directory, two `defs.vh`
    were renamed apart and `rtl/top.v` no longer had its own beside it -- the remote built
    another design, and hashed it as one."""
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    (root / "inc").mkdir()
    (root / "rtl" / "top.v").write_text('`include "defs.vh"\nmodule top; endmodule\n')
    (root / "rtl" / "defs.vh").write_text("`define VAL 1\n")
    (root / "inc" / "defs.vh").write_text("`define VAL 2\n")
    design = Design(
        name="inc",
        design_root=root,
        rtl={"sources": ["rtl/top.v", "rtl/defs.vh", "inc/defs.vh"], "top": "top"},
    )
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        archive.extractall(remote_dir)
    remote = Design.from_file(remote_dir / design_name)

    assert [remote.source_path_as_named(src).as_posix() for src in remote.rtl.sources] == [
        "rtl/top.v",
        "rtl/defs.vh",
        "inc/defs.vh",
    ]
    assert remote.rtl_hash == design.rtl_hash


def test_plain_testbench_parameters_do_not_need_a_newer_remote(tmp_path, monkeypatch):
    """No example has testbench generics, so the sweep above cannot see this case."""
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    shutil.copy(SQRT / "sqrt.vhdl", tmp_path)
    design = Design(
        name="sqrt",
        design_root=tmp_path,
        rtl={"sources": ["sqrt.vhdl"], "top": "sqrt", "parameters": {"G_N": 8}},
        tb={"sources": ["sqrt.vhdl"], "top": "sqrt", "parameters": {"G_N": 8}},
    )
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        shipped = json.loads(archive.read(design_name))

    assert shipped["tb"]["parameters"] == {"G_N": 8}
    assert set(shipped["rtl"]) <= RELEASED_RTL_KEYS
    assert set(shipped["tb"]) <= RELEASED_TB_KEYS


def test_a_remote_run_comes_back_whole_and_hashed_as_it_was_sent(tmp_path, remote_host):
    """A remote run comes back whole and hashed as it was sent."""
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "rom.mem").write_text("00 11\n")
    (design_root / "sqrt.toml").write_text(
        'name = "sqrt"\n'
        "[rtl]\n"
        'sources = ["sqrt.vhdl"]\n'
        'top = "sqrt"\n'
        'clock.port = "clk"\n'
        "[rtl.parameters]\n"
        'G_IN_WIDTH = 32\nROM = { file = "rom.mem" }\nTRACE = { path = "out/trace.txt" }\n'
    )
    local_run_dir = tmp_path / "local" / "xeda_run"  # deliberately not under the start directory

    results = RemoteRunner(local_run_dir).run_remote(
        design_root / "sqrt.toml",
        "vivado_synth",
        host="somewhere",
        flow_settings=["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"],
    )

    assert results and results["success"], results
    remote_run = _remote_run_dir(remote_host, "vivado_synth")
    local_run = Path(results["run_path"])
    assert local_run.is_relative_to(local_run_dir), "the run is reported where it now is"

    # One run, one identity: the remote hashes exactly what this side hashed -- although it
    # unpacked everything under another root, with the sources in a flat archive.
    local_settings = json.loads((local_run / "settings.json").read_text())
    remote_settings = json.loads((remote_run / "settings.json").read_text())
    for key in ("design_hash", "flowrun_hash", "rtl_hash"):
        assert local_settings[key] == remote_settings[key], key

    # The local record is a re-runnable input: its design loads here, as the same design.
    recorded = Design(**local_settings["design"])
    assert recorded.rtl_hash == local_settings["rtl_hash"]

    # A file-valued parameter reached the remote's tool as a file in the remote's own tree.
    remote_parameters = remote_settings["design"]["rtl"]["parameters"]
    rom = Path(remote_parameters["ROM"])
    assert rom.is_relative_to(remote_host) and rom.read_text() == "00 11\n"
    assert Path(remote_parameters["TRACE"]).is_relative_to(remote_host)

    # Artifacts were fetched, and the reported paths rewritten to the local copies.
    artifacts = results["artifacts"]
    paths = list(artifacts.values()) if isinstance(artifacts, dict) else list(artifacts)
    assert paths, "the flow reported artifacts to fetch"
    for path in paths:
        if isinstance(path, str):
            assert Path(path).is_relative_to(local_run / "artifacts"), path
            assert Path(path).exists(), path
    assert json.loads((local_run / "results.json").read_text())["run_path"] == str(local_run)


def test_a_remote_simulation_reads_and_writes_its_file_parameters(tmp_path, remote_host):
    """With a real simulator: the testbench opens the file its `ROM` generic names, and writes
    to the one `TRACE` names -- a path the remote made a place for, not a file that travelled."""
    require_ghdl()
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "rom.mem").write_text("00 11\n")
    (design_root / "dut.vhd").write_text(
        "entity dut is end;\narchitecture a of dut is\nbegin\nend;\n"
    )
    (design_root / "tb.vhd").write_text(
        "use std.textio.all;\n"
        "entity tb is generic (ROM : string; TRACE : string); end;\n"
        "architecture a of tb is\nbegin\n"
        "  process\n"
        "    file f : text; file o : text; variable l : line; variable s : string(1 to 5);\n"
        "  begin\n"
        "    file_open(f, ROM, read_mode); readline(f, l); read(l, s);\n"
        '    assert s = "00 11" report "unexpected ROM: " & s severity failure;\n'
        '    file_open(o, TRACE, write_mode); write(l, string\'("seen ") & s); writeline(o, l);\n'
        "    wait;\n"
        "  end process;\n"
        "end;\n"
    )
    (design_root / "d.toml").write_text(
        'name = "d"\n'
        '[rtl]\nsources = ["dut.vhd"]\ntop = "dut"\n'
        '[tb]\nsources = ["tb.vhd"]\ntop = "tb"\n'
        '[tb.parameters]\nROM = { file = "rom.mem" }\nTRACE = { path = "trace.txt" }\n'
    )

    results = RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "d.toml", "ghdl_sim", host="somewhere"
    )

    assert results and results["success"], results
    remote_settings = json.loads(
        (_remote_run_dir(remote_host, "ghdl_sim") / "settings.json").read_text()
    )
    trace = Path(remote_settings["design"]["tb"]["parameters"]["TRACE"])
    assert trace.is_relative_to(remote_host)
    assert trace.read_text().strip() == "seen 00 11"
    assert not (design_root / "trace.txt").exists(), "the local tree is not the remote's"


def test_remote_ghdl_synth_fetches_list_artifacts(tmp_path, remote_host):
    """GHDL per-source synthesis reports a list under generated_verilog."""
    require_ghdl()
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.toml").write_text(
        'name = "sqrt"\nlanguage.vhdl.standard = "2008"\n'
        '[rtl]\nsources = ["sqrt.vhdl"]\ntop = "sqrt"\n'
    )

    results = RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "sqrt.toml",
        "ghdl_synth",
        host="somewhere",
        flow_settings=["verilog_output=vout"],
    )

    assert results and results["success"], results
    generated = results["artifacts"]["generated_verilog"]
    assert isinstance(generated, list) and len(generated) == 1
    assert Path(generated[0]).is_file()
    assert Path(generated[0]).is_relative_to(Path(results["run_path"]) / "artifacts")
    saved = json.loads((Path(results["run_path"]) / "results.json").read_text())
    assert saved["artifacts"]["generated_verilog"] == generated


def test_transfer_nested_artifacts_keeps_external_paths_local(tmp_path):
    remote_run = tmp_path / "remote" / "run"
    (remote_run / "reports").mkdir(parents=True)
    (remote_run / "reports" / "report.txt").write_text("internal")
    outside = tmp_path / "remote" / "report.txt"
    outside.write_text("external")
    absolute_outside = tmp_path / "elsewhere" / "report.txt"
    absolute_outside.parent.mkdir()
    absolute_outside.write_text("absolute")
    artifacts = {
        "nested": [
            "reports/report.txt",
            ("../report.txt", str(absolute_outside), Path("reports/report.txt")),
        ],
        "empty": "",
        "metadata": None,
    }
    local_dir = tmp_path / "local" / "artifacts"

    transferred = remote_module._transfer_artifacts(
        _LocalConnection("somewhere"), artifacts, str(remote_run), local_dir
    )

    assert isinstance(transferred["nested"], list)
    assert isinstance(transferred["nested"][1], tuple)
    assert transferred["empty"] == "" and transferred["metadata"] is None
    paths = [transferred["nested"][0], *transferred["nested"][1]]
    assert all(Path(path).is_relative_to(local_dir) for path in paths)
    assert [Path(path).read_text() for path in paths] == [
        "internal",
        "external",
        "absolute",
        "internal",
    ]
    assert paths[0] == paths[3], "the repeated source needs only one local copy"
    assert len(set(paths)) == 3, "same-named external files must remain distinct"


def test_transfer_rejects_artifact_destination_symlink(tmp_path):
    remote_run = tmp_path / "remote"
    (remote_run / "reports").mkdir(parents=True)
    (remote_run / "reports" / "report.txt").write_text("remote")
    local_dir = tmp_path / "local" / "artifacts"
    local_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (local_dir / "reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside"):
        remote_module._transfer_artifacts(
            _LocalConnection("somewhere"),
            {"report": "reports/report.txt"},
            str(remote_run),
            local_dir,
        )

    assert list(outside.iterdir()) == []


def test_the_remote_sends_back_results_whatever_their_keys(tmp_path, monkeypatch):
    """`remote_runner` encodes the remote's results for the trip back. `json` raises on a key it
    cannot write (a tuple, a `Path`), which lost a remote run's results entirely; the function
    runs against the remote's xeda, so it applies the key rule itself, in plain Python."""
    import xeda.flow_runner

    class Results(dict):
        def to_dict(self):
            return dict(self)

    class Launcher:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(
                results=Results(success=True, timing={("clk", "rise"): 1.5, Path("/x.v"): 2})
            )

    class Channel:
        sent: list = []

        def isclosed(self):
            return False

        def send(self, value):
            self.sent.append(value)

    monkeypatch.setattr(xeda.flow_runner, "DefaultRunner", Launcher)
    # `remote_runner` changes into the run directory, as it must on the remote; `monkeypatch`
    # restores this process's working directory afterwards, which later tests rely on.
    monkeypatch.chdir(tmp_path)
    with zipfile.ZipFile(tmp_path / "d.zip", "w") as archive:
        archive.writestr("d.xeda.json", "{}")
    channel = Channel()
    remote_module.remote_runner(channel, str(tmp_path), "d.zip", "flow", "d.xeda.json", {})

    (sent,) = channel.sent
    assert json.loads(sent)["timing"] == {"('clk', 'rise')": 1.5, "/x.v": 2}
