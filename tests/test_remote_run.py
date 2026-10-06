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
import logging
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import execnet
import pytest

from pydantic import ValidationError

from xeda import Design
from xeda.design import GitReference, SourceType
from xeda.deliver import DeliveryError
from xeda.flow import FlowException, FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DIR_NAME_HASH_LEN, get_flow_class
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.remote import RemoteIncompatible, RemoteRunner
from xeda.flow_runner.default_runner import ProjectFileError
from xeda.flow_runner.run_lock import lock_file
from xeda.run_root import RunRootError, ensure_run_root, is_run_root
from xeda.xedaproject import PROJECT_FILE_NAMES

from .project_files import PROJECT_FILE, TOML_PROJECT_FILE
from .tool_utils import require_ghdl, require_yosys

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


class _LocalSftp:
    def mkdir(self, path):
        os.mkdir(path)

    def chdir(self, path):
        assert os.path.isdir(path), path

    def open(self, path, mode="r"):
        return open(path, mode)

    def stat(self, path):
        return os.stat(path)  # paramiko's raises FileNotFoundError too


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
    # The worker changes directory: a relative PYTHONPATH=src would instead find the venv's
    # editable install, which may point at main rather than the checkout under test.
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR.parent / "src"))
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
    (settings,) = home.glob(f".xeda/remote_run/*/xeda_run/*/{flow_name}*/settings.json")
    return settings.parent


def test_remote_rebuild_all_forces_only_local_generation_before_transport(tmp_path, monkeypatch):
    """Remote's default `clean=True` keeps flows fresh; only explicit rebuild forces generation."""
    from .io_flows import _Maker

    root = tmp_path / "design"
    root.mkdir()
    counter = tmp_path / "generator-runs"
    (root / "gen.py").write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "root = Path(os.environ['DESIGN_ROOT'])\n"
        "(root / 'gen').mkdir(exist_ok=True)\n"
        "(root / 'gen' / 'top.v').write_text('module top; endmodule\\n')\n"
        "Path(sys.argv[1]).open('a').write('ran\\n')\n"
    )
    (root / "design.yaml").write_text(
        "name: remote-generated\n"
        "rtl:\n"
        "  sources: [gen/top.v]\n"
        "  top: top\n"
        "  generator:\n"
        f"    executable: {sys.executable!r}\n"
        "    args: [gen.py, " + repr(str(counter)) + "]\n"
        "    sources: [gen.py]\n"
    )

    class StopBeforeTransport(Exception):
        pass

    monkeypatch.setattr(
        remote_module, "Connection", lambda **_kwargs: (_ for _ in ()).throw(StopBeforeTransport())
    )
    local_root = tmp_path / "local-run-root"

    def attempt(rebuild_all=False):
        runner = RemoteRunner(local_root, display_results=False, rebuild_all=rebuild_all)
        with pytest.raises(StopBeforeTransport):
            runner.run_remote(root / "design.yaml", _Maker.name, "host")

    attempt()
    assert len(counter.read_text().splitlines()) == 1
    attempt()
    assert len(counter.read_text().splitlines()) == 1, "remote clean forced ordinary generation"
    attempt(rebuild_all=True)
    assert len(counter.read_text().splitlines()) == 2


@pytest.mark.parametrize("location", ["design", "cwd", "external"])
def test_declared_remote_ships_a_producers_settings_only_file(
    tmp_path, remote_host, monkeypatch, location
):
    from .io_flows import _InputTaker

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    root = tmp_path / "design"
    root.mkdir()
    input_path = {
        "design": root / "input.txt",
        "cwd": Path.cwd() / "input.txt",
        "external": tmp_path / "input.txt",
    }[location]
    input_path.write_text("from settings\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": [], "top": "t"},
        flow={"__input_maker": {"input_file": str(input_path)}},
    )
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    expected = runner.resolve(_InputTaker, design, {}, design.flow)
    results = runner.run_remote(design, _InputTaker.name, "fake")
    assert results and results["success"] and results["read"] == "from settings\n"
    producer = _remote_run_dir(remote_host, "__input_maker")
    settings = json.loads((producer / "settings.json").read_text())
    input_file = Path(settings["flow_settings"]["input_file"])
    assert input_file.is_relative_to(remote_host) and input_file.read_text() == "from settings\n"
    assert settings["flowrun_hash"] == expected.node("__input_maker").flowrun_hash


@pytest.mark.parametrize("source", [False, True])
def test_remote_executes_a_declared_graph_with_the_same_input_origin(
    tmp_path, remote_host, monkeypatch, source
):
    from .io_flows import _Taker

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    root = tmp_path / "design"
    root.mkdir()
    entries = []
    if source:
        (root / "given.dat").write_text("given\n")
        entries = [{"file": "given.dat", "type": "Data"}]
    design = Design(name="d", design_root=root, rtl={"sources": entries, "top": "t"})
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    expected = runner.resolve(_Taker, design, {})
    results = runner.run_remote(design, _Taker.name, "fake")
    assert results["success"] and results["read"] == ("given\n" if source else "made\n")
    remote_path = _remote_run_dir(remote_host, _Taker.name)
    trace = json.loads((remote_path / "trace.json").read_text())
    assert trace["declared_inputs"][0]["origin"] == expected.node(_Taker.name).inputs[0].origin
    assert results["flow_hash"] == expected.node(_Taker.name).flowrun_hash


def test_remote_mirrors_and_delivers_declared_outputs_without_artifact_labels(
    tmp_path, remote_host, monkeypatch
):
    from .io_flows import _Maker
    from xeda.digest import content_digest

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = RemoteRunner(tmp_path / "mirror", display_results=False, outputs_to=tmp_path / "out")
    results = runner.run_remote(design, _Maker.name, "fake")
    recorded = results["outputs"]["made"]
    mirrored = Path(recorded["path"])
    assert mirrored.is_relative_to(tmp_path / "mirror")
    assert content_digest(mirrored) == recorded["sha"]
    assert mirrored.read_text() == (tmp_path / "out" / "made.txt").read_text() == "made\n"


def test_remote_declared_output_collision_is_refused_before_connecting(
    tmp_path, remote_host, monkeypatch
):
    from .io_flows import _Maker
    from xeda.deliver import OutputExistsError

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    root = tmp_path / "mirror"
    first = RemoteRunner(root, display_results=False).run_remote(design, _Maker.name, "fake")
    assert first and first["success"] and first["outputs"] and not first.get("artifacts")
    destination = tmp_path / "out"
    destination.mkdir()
    conflict = destination / "made.txt"
    conflict.write_text("user's file\n")
    monkeypatch.setattr(remote_module, "Connection", lambda *a, **k: pytest.fail("it connected"))

    with pytest.raises(OutputExistsError, match="made.txt"):
        RemoteRunner(root, display_results=False, outputs_to=destination).run_remote(
            design, _Maker.name, "fake"
        )
    assert conflict.read_text() == "user's file\n"


@pytest.mark.parametrize("stage", ["init", "producer"])
def test_remote_declared_setup_failures_return_the_failure_document(
    tmp_path, remote_host, monkeypatch, stage
):
    from .io_flows import _Taker

    target = "_Taker.init" if stage == "init" else "_Maker.run"
    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\n"
        "import tests.io_flows as io\nfrom xeda.flow import FlowFatalError\n"
        "def broken(self):\n    raise FlowFatalError('broken setup')\n"
        f"io.{target} = broken\n" + remote_module.REMOTE_PROBE,
    )
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    results = RemoteRunner(tmp_path / "mirror", outputs_to=tmp_path / "out").run_remote(
        design, _Taker.name, "fake"
    )
    assert results is not None and results["success"] is False
    assert "broken setup" in results["error"]["message"]
    assert results["error"]["type"] == (
        "FlowFatalError" if stage == "init" else "FlowDependencyFailure"
    )
    for key in ("design", "design_hash", "flow", "flow_hash", "run_path", "timestamp"):
        assert results[key]
    assert not (tmp_path / "out").exists()


def test_declared_remote_outputs_are_verified_before_delivery(tmp_path, remote_host, monkeypatch):
    from .io_flows import _Maker

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    get = _LocalConnection.get

    def changed(self, remote, local):
        result = get(self, remote, local)
        if Path(remote).name == "made.txt":
            Path(local).write_text("changed in transfer\n")
        return result

    monkeypatch.setattr(_LocalConnection, "get", changed)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    with pytest.raises(DeliveryError, match="changed during transfer"):
        RemoteRunner(tmp_path / "mirror", outputs_to=tmp_path / "out").run_remote(
            design, _Maker.name, "fake"
        )
    assert not (tmp_path / "out").exists()


def test_failed_remote_partial_outputs_are_not_handed_over(tmp_path, remote_host, monkeypatch):
    from .io_flows import _PartialMaker

    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    results = RemoteRunner(tmp_path / "mirror", outputs_to=tmp_path / "out").run_remote(
        design, _PartialMaker.name, "fake"
    )
    assert results is not None and results["success"] is False
    assert results["error"]["type"] == "MissingOutput"
    assert not results.get("outputs")
    assert not (tmp_path / "out").exists()


def test_remote_declared_shared_settings_agree_before_connecting(tmp_path, monkeypatch):
    from .io_flows import _Place

    connected = []

    def connection(**kwargs):
        connected.append(kwargs)
        raise AssertionError("invalid graph connected to the remote")

    monkeypatch.setattr(remote_module, "Connection", connection)
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [], "top": "t"},
        flow={
            "__place": {"fpga": "LFE5U-25F-6BG256C"},
            "__synth": {"fpga": "LFE5U-85F-6BG381C"},
        },
    )
    with pytest.raises(FlowSettingsError, match="disagree"):
        RemoteRunner(tmp_path / "mirror").run_remote(design, _Place.name, "fake")
    assert not connected and not (tmp_path / "mirror").exists()


@pytest.mark.parametrize(
    "version,protocol,flow_name,flow_settings",
    [
        ("0.4.3", 0, "vivado_synth", ["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"]),
        ("0.4.4.dev1", 2, "ghdl_sim", None),
        ("0.4.4.dev1", 3, "fpga_pack", ["fpga.part=LFE5U-25F-6BG381C"]),
        ("0.4.4.dev1", 4, "openfpgaloader", ["fpga.part=LFE5U-25F-6BG381C"]),
        # protocol 5 predates D-9's node identity: its `flow_hash` for the same request differs
        # from this side's, so it must be refused here, not accepted and failed on a mirror hash
        ("0.4.4.dev1", 5, "nextpnr", ["fpga.part=LFE5U-25F-6BG381C"]),
        ("0.4.4.dev1", 5, "ghdl_sim", None),
        ("0.4.4.dev1", 6, "ghdl_sim", None),
        # protocol 7 (declared Vivado outputs) predates PCD23: it hashes a bundled platform by
        # its own installation's paths, and configures `yosys` without its platform
        ("0.4.4.dev1", 7, "yosys", ["platform=nangate45", "clock.period=2.0"]),
        ("0.4.4.dev1", 7, "ghdl_sim", None),
    ],
)
def test_a_remote_without_required_protocol_is_refused_before_anything_ships(
    tmp_path, remote_host, monkeypatch, version, protocol, flow_name, flow_settings
):
    """Reject a pre-P2a release or a remote of an older protocol before shipping the design."""
    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        "import xeda\n"
        "from importlib import metadata\n"
        f"xeda.__version__ = {version!r}\n"
        f"xeda.REMOTE_PROTOCOL_VERSION = {protocol}\n"
        f"metadata.version = lambda name: {version!r}\n" + remote_module.REMOTE_PROBE,
    )
    shipped = []
    closed = []
    real_makegateway = remote_module.execnet.makegateway

    def makegateway(spec):
        gateway = real_makegateway(spec)
        real_exit = gateway.exit

        def exit():
            closed.append("gateway")
            real_exit()

        gateway.exit = exit
        return gateway

    def ship(*args, **kwargs):
        shipped.append(True)
        raise AssertionError("the incompatible remote was sent a design")

    monkeypatch.setattr(remote_module, "send_design", ship)
    monkeypatch.setattr(remote_module.execnet, "makegateway", makegateway)
    monkeypatch.setattr(_LocalConnection, "close", lambda self: closed.append("connection"))
    design = _sqrt_design(tmp_path / "design")
    with pytest.raises(RemoteIncompatible, match="upgrade the remote xeda") as raised:
        RemoteRunner(tmp_path / "local").run_remote(
            design, flow_name, host="somewhere", flow_settings=flow_settings
        )
    assert version in str(raised.value)
    assert "P3" in str(raised.value)
    assert f"remote protocol {protocol}" in str(raised.value)
    assert "remote protocol 8 or newer" in str(raised.value)
    assert not shipped
    assert sorted(closed) == ["connection", "gateway"]


#: The archive accepted by a P3 remote (protocol 8), including this branch's dev builds.
#: Keep these pins explicit: an incompatible archive change requires a protocol-floor bump;
#: a release raises REMOTE_XEDA_MIN_VERSION as CLAUDE.md describes.
P2A_RTL_KEYS = {
    "attributes",
    "clocks",
    "defines",
    "generator",
    "generics",
    "parameters",
    "sources",
    "top",
}
P2A_TB_KEYS = {"cocotb", "defines", "generics", "parameters", "sources", "top", "uut"}

EXAMPLE_DESIGNS = sorted(
    p
    for p in (TESTS_DIR.parent / "examples").rglob("*")
    if p.suffix in (".toml", ".yaml", ".yml")
    and not p.name.startswith("xedaproject.")
    and "xeda_run" not in p.parts
)


@pytest.mark.parametrize("design_file", EXAMPLE_DESIGNS, ids=lambda p: p.name)
def test_the_design_archive_is_readable_by_a_p2a_remote(design_file, tmp_path, monkeypatch):
    """The design archive uses the keys accepted by a P2a remote."""
    design = Design.from_file(design_file)
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        shipped = json.loads(archive.read(design_name))

    assert set(shipped["rtl"]) <= P2A_RTL_KEYS
    assert set(shipped["tb"]) <= P2A_TB_KEYS


TARGETS = TESTS_DIR / "resources" / "targets"


def test_a_remote_is_sent_the_design_a_target_yields(tmp_path, monkeypatch):
    """The archive of a design with a target selected is that of the same design written flat:
    neither `targets` nor the recorded `target`, which a remote's loader would refuse."""
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)

    def shipped(design: Design, name: str) -> dict:
        remote_dir = tmp_path / name
        remote_dir.mkdir()
        zip_name, design_name = remote_module.send_design(
            design, _LocalConnection("somewhere"), str(remote_dir)
        )
        with zipfile.ZipFile(remote_dir / zip_name) as archive:
            return json.loads(archive.read(design_name))

    selected = shipped(Design.from_file(TARGETS / "knight.yaml", target="arty"), "selected")
    flat = shipped(Design.from_file(TARGETS / "knight_arty_flat.yaml"), "flat")
    assert "target" not in selected and "targets" not in selected
    assert selected == flat


def test_a_remote_run_takes_a_target(tmp_path, remote_host):
    runner = RemoteRunner(tmp_path / "local" / "xeda_run")
    results = runner.run_remote(
        TARGETS / "knight.yaml", "vivado_synth", host="somewhere", target="arty"
    )
    assert results is not None and results["success"] is True
    assert runner.target == "arty"
    with pytest.raises(Exception, match="arty, ulx3s"):
        RemoteRunner(tmp_path / "other" / "xeda_run").run_remote(
            TARGETS / "knight.yaml", "vivado_synth", host="somewhere"
        )


#: A P2a design dependency (`GitReference`) and the keys it accepts as `null`.
#: Unlike the old floor, P2a accepts an unset local_cache directly.
P2A_GIT_REFERENCE_KEYS = {
    "uri",
    "rtl",
    "tb",
    "local_cache",
    "repo_url",
    "design_file",
    "commit",
    "branch",
    "clone_dir",
}
P2A_NULLABLE_GIT_REFERENCE_KEYS = {"commit", "branch", "clone_dir", "local_cache"}


@pytest.mark.parametrize("local_cache", [None, "deps"])
def test_a_git_dependency_is_archived_as_a_p2a_remote_reads_it(local_cache, tmp_path, monkeypatch):
    """A loaded design holds its dependencies' sources itself and ships no dependency; one
    assigned afterwards is shipped for the remote to fetch, in the form P2a reads."""
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    design = Design.from_file(_sqrt_design(tmp_path / "design"))
    reference = {"uri": "https://example.com/org/repo.git?branch=dev#design.yaml"}
    if local_cache is not None:
        reference["local_cache"] = str(tmp_path / local_cache)
    design.dependencies = [GitReference(**reference)]
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        (shipped,) = json.loads(archive.read(design_name))["dependencies"]

    assert set(shipped) <= P2A_GIT_REFERENCE_KEYS
    assert {k for k, v in shipped.items() if v is None} <= P2A_NULLABLE_GIT_REFERENCE_KEYS
    assert shipped.get("local_cache") == reference.get("local_cache")
    assert shipped["repo_url"] == "https://example.com/org/repo.git"


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
    assert set(shipped["rtl"]) <= P2A_RTL_KEYS
    assert set(shipped["tb"]) <= P2A_TB_KEYS


def test_a_remote_run_comes_back_whole_and_hashed_as_it_was_sent(tmp_path, remote_host):
    """A remote run comes back whole and hashed as it was sent."""
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "rom.mem").write_text("00 11\n")
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\n"
        "rtl:\n"
        "  sources: [sqrt.vhdl]\n"
        "  top: sqrt\n"
        "  clock: {port: clk}\n"
        "  parameters:\n"
        "    G_IN_WIDTH: 32\n"
        "    ROM: {file: rom.mem}\n"
        "    TRACE: {path: out/trace.txt}\n"
    )
    # deliberately not under the start directory
    local_run_dir = ensure_run_root(tmp_path / "local" / "xeda_run")
    assert local_run_dir is not None
    # a local run in the default (stable) layout, whose directory the mirror must not touch
    local_trace = local_run_dir / "sqrt" / "vivado_synth" / "trace.json"
    local_trace.parent.mkdir(parents=True)
    local_trace.write_text("{}")

    def run_remote():
        return RemoteRunner(local_run_dir).run_remote(
            design_root / "sqrt.yaml",
            "vivado_synth",
            host="somewhere",
            flow_settings=["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"],
        )

    results = run_remote()

    assert results and results["success"], results
    remote_run = _remote_run_dir(remote_host, "vivado_synth")
    local_run = Path(results["run_path"])
    assert local_run.is_relative_to(local_run_dir), "the run is reported where it now is"
    assert local_trace.read_text() == "{}", "a local run's directory is not the mirror"

    # One run, one identity: the remote hashes exactly what this side hashed -- although it
    # unpacked everything under another root, with the sources in a flat archive.
    local_settings = json.loads((local_run / "settings.json").read_text())
    remote_settings = json.loads((remote_run / "settings.json").read_text())
    # the mirror is hashed, whatever layout local runs use: `<flow>_<flowrun_hash>`
    flowrun_hash = local_settings["flowrun_hash"]
    assert local_run == local_run_dir / "sqrt" / f"vivado_synth_{flowrun_hash[:DIR_NAME_HASH_LEN]}"
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

    # A local `--hashed-run-dirs` run of the same settings shares the mirror's directory: its
    # trace must not vouch for the remote run's results.
    (local_run / "trace.json").write_text("{}")
    again = run_remote()
    assert again and again["success"] and Path(again["run_path"]) == local_run
    assert not (local_run / "trace.json").exists()
    assert list((tmp_path / "started_in").iterdir()) == [], "the start directory stays empty"


def test_a_remote_run_is_always_mirrored_in_hashed_run_directories(tmp_path):
    """Remote runs of different settings never share a local directory, nor a local run's."""
    assert RemoteRunner(tmp_path / "xeda_run").settings.hashed_run_dirs is True
    with pytest.raises(ValidationError, match="hashed_run_dirs"):
        RemoteRunner(tmp_path / "xeda_run", hashed_run_dirs=False)


def _run_vivado_alt_synth_with_netlist(tmp_path: Path) -> dict | None:
    """`vivado_alt_synth`, which records its netlist before Vivado runs, run remotely on the fake
    Vivado, which writes no netlist."""
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl:\n  sources: [sqrt.vhdl]\n  top: sqrt\n  clock: {port: clk}\n"
    )
    return RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "sqrt.yaml",
        "vivado_alt_synth",
        host="somewhere",
        flow_settings=[
            "fpga.part=xc7a12tcsg325-1",
            "clock.period=5.0",
            "write_netlist=true",
            "write_timing_netlist=true",
        ],
    )


NETLIST_ARTIFACTS = {
    "netlist": "outputs/impl_funcsim.v",
    "netlist_timing": "outputs/impl_timesim.v",
    "sdf": "outputs/impl_timesim.sdf",
    "xdc_exported": "outputs/impl.xdc",
}


def test_a_failed_remote_run_reports_its_failure(tmp_path, remote_host, monkeypatch):
    """When the run failed, its results still listed the netlist, and fetching that missing file
    raised `FileNotFoundError`: a failed remote run crashed instead of reporting its failure."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "route_design")  # inherited by the "remote"

    results = _run_vivado_alt_synth_with_netlist(tmp_path)

    assert results is not None and results["success"] is False, results
    for label in NETLIST_ARTIFACTS:
        assert label not in results["artifacts"], label
    saved = json.loads((Path(results["run_path"]) / "results.json").read_text())
    assert saved["success"] is False


def _remote_runner_with_unwritten_artifacts(channel, **kwargs):
    """Inject unfiltered artifact reporting into a supported worker to exercise transport
    defenses. execnet ships this function's source alone, so it imports what it patches."""
    from xeda.flow_runner import default_runner
    from xeda.flow_runner.remote import remote_runner

    default_runner._drop_unwritten_artifacts = id  # a no-op of one argument
    remote_runner(channel, **kwargs)


def _remote_runner_listing_an_artifact_it_never_wrote(channel, **kwargs):
    """Inject a flow that succeeds, with every declared output written, and lists a plain
    artifact it did not write. execnet ships this function's source alone."""
    from pathlib import Path

    from xeda.flow_runner.remote import remote_runner
    from xeda.flows import VivadoAltSynth

    # as source: execnet refuses a shipped function that has nested functions or free names
    exec(
        "original = VivadoAltSynth.run\n"
        "def run(self):\n"
        "    original(self)\n"
        "    self.artifacts['ghost'] = Path('outputs/ghost.txt')\n"
        "VivadoAltSynth.run = run\n",
        {"VivadoAltSynth": VivadoAltSynth, "Path": Path},
    )
    remote_runner(channel, **kwargs)


@pytest.mark.parametrize("fails", [True, False], ids=["failed", "succeeded"])
def test_a_remote_listing_unwritten_artifacts_is_handled(
    fails, tmp_path, remote_host, monkeypatch, caplog
):
    """A faulty worker reporting unwritten artifacts cannot make the transport fetch them
    after failure; after success, a missing artifact is still an error. (A declared output a
    run did not write fails the run itself, `MissingOutput`, before any transport: the artifact
    after success is a plain one.)"""
    if not fails:
        monkeypatch.setattr(
            remote_module, "remote_runner", _remote_runner_listing_an_artifact_it_never_wrote
        )
        with pytest.raises(FileNotFoundError, match=r"ghost\.txt"):
            _run_vivado_alt_synth_with_netlist(tmp_path)
        return
    monkeypatch.setattr(remote_module, "remote_runner", _remote_runner_with_unwritten_artifacts)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "route_design")

    with caplog.at_level(logging.WARNING):
        results = _run_vivado_alt_synth_with_netlist(tmp_path)

    assert results is not None and results["success"] is False, results
    for label in NETLIST_ARTIFACTS:
        assert label not in results["artifacts"], label
    saved = json.loads((Path(results["run_path"]) / "results.json").read_text())
    assert saved["success"] is False and saved["artifacts"] == results["artifacts"]
    (warning,) = [r.getMessage() for r in caplog.records if "did not write" in r.getMessage()]
    assert warning.startswith("vivado_alt_synth failed")
    for label, path in NETLIST_ARTIFACTS.items():
        assert f"{label}: {path}" in warning


def _remote_runner_without_wrote_output(channel, **kwargs):
    """Inject a missing write-reporting API as well as unfiltered artifacts into the worker.
    This exercises a defensive fallback, not acceptance of a pre-P2a remote."""
    from xeda.flow import Flow
    from xeda.flow_runner import default_runner
    from xeda.flow_runner.remote import remote_runner

    default_runner._drop_unwritten_artifacts = id  # a no-op of one argument
    del Flow.wrote_output
    remote_runner(channel, **kwargs)


REMOTES = {
    "this xeda": None,
    "unfiltered artifacts": _remote_runner_with_unwritten_artifacts,
    "missing wrote_output": _remote_runner_without_wrote_output,
}


@pytest.mark.parametrize("remote", sorted(REMOTES))
def test_a_failed_remote_run_does_not_fetch_an_earlier_runs_output(
    remote, tmp_path, remote_host, monkeypatch
):
    """A failed remote run's outputs it never wrote are neither fetched nor listed: the remote
    judges what its failed run wrote on its own file system, and this side keeps only what it
    vouched for, never a file merely because the results name it. (An output named on the remote
    outside its run directory, which an earlier run could have left there, is refused before
    anything is shipped: `--remote` takes no deliverable location. The remote's run directory
    itself is fresh each run, under a new time-stamped directory.)"""
    if REMOTES[remote] is not None:
        monkeypatch.setattr(remote_module, "remote_runner", REMOTES[remote])
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "launch_runs")  # inherited by the "remote"
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl:\n  sources: [sqrt.vhdl]\n  top: sqrt\n  clock: {port: clk}\n"
    )

    results = RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "sqrt.yaml",
        "vivado_synth",
        host="somewhere",
        flow_settings=[
            "fpga.part=xc7a12tcsg325-1",
            "clock.period=5.0",
            "bitstream=top.bit",
        ],
    )

    assert results is not None and results["success"] is False, results
    assert "bitstream" not in results["artifacts"], results["artifacts"]
    local_run = Path(results["run_path"])
    assert not list(local_run.rglob("top.bit")), "a bitstream the run never wrote was fetched"
    saved = json.loads((local_run / "results.json").read_text())
    assert "bitstream" not in saved["artifacts"]


class _Channel:
    """An execnet channel's sending end, recording what is sent."""

    def __init__(self):
        self.sent: list = []

    def isclosed(self):
        return False

    def send(self, value):
        self.sent.append(value)


@pytest.mark.parametrize("remote", ["this xeda", "missing wrote_output"])
def test_the_remote_vouches_only_for_what_its_failed_run_wrote(remote, tmp_path, monkeypatch):
    """`remote_runner` itself, run here as the remote runs it: after a failed run it sends back
    which of the run's artifacts the run wrote, judged on the remote's own file system -- by the
    remote flow's own `wrote_output` where it has one, and otherwise by the state of what the
    remote directory held before the run. A file the run wrote is vouched for; one it never
    wrote, a file the design archive brought, and one outside the remote directory that it cannot
    judge are not."""
    from xeda.flow import Flow, registered_flows
    from xeda.flow_runner import default_runner

    remote_path = tmp_path / "remote" / "220101"
    remote_path.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "stale.bit"
    outside.parent.mkdir()
    outside.write_text("an earlier run's\n")
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    zip_file, design_file = remote_module.send_design(
        Design.from_file(SQRT / "sqrt.yaml"), _LocalConnection("somewhere"), str(remote_path)
    )
    if remote != "this xeda":
        monkeypatch.setattr(default_runner, "_drop_unwritten_artifacts", id)
        monkeypatch.delattr(Flow, "wrote_output")

    class WritesOneAndFails(Flow):
        """Write one of the outputs it declares, then fail."""

        results_description = {}

        def run(self):
            (self.run_path / "written.v").write_text("this run's\n")
            self.artifacts.written = "written.v"
            self.artifacts.never = "never.v"
            self.artifacts.outside = str(outside)
            self.artifacts.archived = str(remote_path / design_file)

        def parse_reports(self):
            return False

    monkeypatch.chdir(tmp_path)  # `remote_runner` changes into the remote directory
    channel = _Channel()
    try:
        remote_module.remote_runner(
            channel, str(remote_path), zip_file, WritesOneAndFails.name, design_file, {}
        )
    finally:
        for name in (WritesOneAndFails.name, WritesOneAndFails.__name__):
            registered_flows.pop(name, None)

    sent, written = channel.sent
    results = json.loads(sent)
    assert results["success"] is False
    if remote != "this xeda":  # the remote's launcher listed them all
        assert set(results["artifacts"]) == {"written", "never", "outside", "archived"}
    assert json.loads(written) == ["written.v"]


@pytest.mark.parametrize("vouched", [None, [], ["reports/written.txt"]])
def test_a_failed_remote_run_keeps_only_the_artifacts_the_remote_vouched_for(vouched, tmp_path):
    """This side's half: of a failed run's artifacts, only those the remote vouched for are
    fetched and kept -- a file that merely exists on the remote is not, and with no word from the
    remote at all, none is."""
    remote_run = tmp_path / "remote" / "run"
    (remote_run / "reports").mkdir(parents=True)
    for name in ("written.txt", "earlier.txt"):
        (remote_run / "reports" / name).write_text(name)
    local_dir = tmp_path / "local" / "artifacts"

    transferred = remote_module._transfer_artifacts(
        _LocalConnection("somewhere"),
        {"written": "reports/written.txt", "earlier": "reports/earlier.txt"},
        str(remote_run),
        local_dir,
        succeeded=False,
        written_on_remote=vouched,
    )

    if vouched:
        assert list(transferred) == ["written"]
        assert Path(transferred["written"]).read_text() == "written.txt"
    else:
        assert transferred == {}
    assert not list(local_dir.rglob("earlier.txt"))


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
        "    std.env.finish;\n"
        "    wait;\n"
        "  end process;\n"
        "end;\n"
    )
    (design_root / "d.yaml").write_text(
        "name: d\nlanguage: {vhdl: {standard: '2008'}}\n"
        "rtl: {sources: [dut.vhd], top: dut}\n"
        "tb:\n  sources: [tb.vhd]\n  top: tb\n"
        "  parameters: {ROM: {file: rom.mem}, TRACE: {path: trace.txt}}\n"
    )

    results = RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "d.yaml", "ghdl_sim", host="somewhere"
    )

    assert results and results["success"], results
    evidence = results["sim.evidence"]
    assert evidence["ended_by"] == "finish"
    assert results["sim.ended_by"] == "finish"
    remote_results = json.loads(
        (_remote_run_dir(remote_host, "ghdl_sim") / "results.json").read_text()
    )
    assert remote_results["sim.evidence"] == evidence
    assert remote_results["sim.ended_by"] == "finish"
    remote_settings = json.loads(
        (_remote_run_dir(remote_host, "ghdl_sim") / "settings.json").read_text()
    )
    trace = Path(remote_settings["design"]["tb"]["parameters"]["TRACE"])
    assert trace.is_relative_to(remote_host)
    assert trace.read_text().strip() == "seen 00 11"
    assert not (design_root / "trace.txt").exists(), "the local tree is not the remote's"
    assert list((tmp_path / "started_in").iterdir()) == [], "the start directory stays empty"


def test_remote_ghdl_synth_fetches_list_artifacts(tmp_path, remote_host):
    """GHDL per-source synthesis reports a list under generated_verilog."""
    require_ghdl()
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nlanguage: {vhdl: {standard: '2008'}}\n"
        "rtl: {sources: [sqrt.vhdl], top: sqrt}\n"
    )

    results = RemoteRunner(tmp_path / "local" / "xeda_run").run_remote(
        design_root / "sqrt.yaml",
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
    assert list((tmp_path / "started_in").iterdir()) == [], "the start directory stays empty"


def test_a_remote_run_rejects_a_testbench_the_simulator_cannot_run_before_connecting(
    tmp_path, remote_host, monkeypatch
):
    """Here, not on the remote: a remote on a release without the check would run a cocotb
    testbench on modelsim as an ordinary simulation, its tests silently never run."""
    connected_to = []

    class _Unreachable(_LocalConnection):
        def __init__(self, host, user=None, port=None):
            connected_to.append(host)
            raise ConnectionRefusedError(f"connected to {host}")

    monkeypatch.setattr(remote_module, "Connection", _Unreachable)
    local_run_dir = tmp_path / "local" / "xeda_run"

    with pytest.raises(FlowException, match=re.escape("modelsim cannot run cocotb tests")):
        RemoteRunner(local_run_dir).run_remote(SQRT / "sqrt.yaml", "modelsim", host="somewhere")

    assert not connected_to
    assert not (remote_host / ".xeda").exists(), "nothing was shipped"
    assert not local_run_dir.exists(), "nor a run root set up for it"


def test_a_remote_run_refuses_a_users_local_results_directory_before_connecting(
    tmp_path, remote_host, monkeypatch
):
    """The local directory a remote run's results are fetched into is a run directory xeda
    chooses, under the run root: a directory of the user's named as the run root (`--run-root
    myrundir`, with `myrundir/sqrt/vivado_synth/settings.json`) is refused, naming it, before
    connecting -- its `settings.json`, `results.json` and fetched artifacts would overwrite the
    user's files."""
    connected_to = []

    class _Unreachable(_LocalConnection):
        def __init__(self, host, user=None, port=None):
            connected_to.append(host)
            raise ConnectionRefusedError(f"connected to {host}")

    monkeypatch.setattr(remote_module, "Connection", _Unreachable)
    local_run_dir = tmp_path / "myrundir"
    canary = local_run_dir / "sqrt" / "vivado_synth" / "settings.json"
    canary.parent.mkdir(parents=True)
    canary.write_text("the user's own file\n")

    with pytest.raises(RunRootError, match=re.escape(str(local_run_dir))):
        RemoteRunner(local_run_dir).run_remote(
            SQRT / "sqrt.yaml",
            "vivado_synth",
            host="somewhere",
            flow_settings=["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"],
        )

    assert not connected_to
    assert not (remote_host / ".xeda").exists(), "nothing was shipped"
    assert canary.read_text() == "the user's own file\n"
    assert sorted(p.name for p in canary.parent.iterdir()) == ["settings.json"]


def test_a_remote_run_marks_its_local_results_directory_and_reuses_it(tmp_path, remote_host):
    """A remote run's local results directory lies in a run root it marks as xeda's, so the next
    remote run of the flow reuses it."""
    local_run_dir = tmp_path / "local" / "xeda_run"
    mirrors = []
    for attempt in (1, 2):
        results = RemoteRunner(local_run_dir).run_remote(
            SQRT / "sqrt.yaml",
            "vivado_synth",
            host="somewhere",
            flow_settings=["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"],
        )
        assert results and results["success"], f"run {attempt}: {results}"
        local_run = Path(results["run_path"])
        assert local_run.parent == local_run_dir / "sqrt"
        assert local_run.name.startswith("vivado_synth_")
        assert is_run_root(local_run_dir), f"run {attempt}"
        mirrors.append(local_run)
    assert mirrors[0] == mirrors[1]


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


def _run_remote_runner(tmp_path, monkeypatch, results, settings_fields):
    """Run `remote_runner` against a stand-in for the remote's `DefaultRunner`, whose `Settings`
    declares `settings_fields`. What it sent back, the launcher settings it was given, and the
    run root."""
    import xeda.flow_runner

    class Results(dict):
        def to_dict(self):
            return dict(self)

    given: dict = {}
    roots: list = []

    class Launcher:
        class Settings:
            model_fields = dict.fromkeys(settings_fields)

        def __init__(self, *args, **kwargs):
            roots.extend(args[:1])
            given.update(kwargs)

        def run(self, *args, **kwargs):
            return SimpleNamespace(results=Results(results))

    class Channel:
        def __init__(self):
            self.sent: list = []

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
    sent, written = channel.sent  # the results, then which artifacts a failed run wrote
    return sent, written, given, roots


def test_the_remote_sends_back_results_whatever_their_keys(tmp_path, monkeypatch):
    """`remote_runner` encodes the remote's results for the trip back. `json` raises on a key it
    cannot write (a tuple, a `Path`), which lost a remote run's results entirely; the function
    runs against the remote's xeda, so it applies the key rule itself, in plain Python."""
    results = dict(success=True, timing={("clk", "rise"): 1.5, Path("/x.v"): 2})
    sent, written, _, _ = _run_remote_runner(tmp_path, monkeypatch, results, ["hashed_run_dirs"])
    assert json.loads(sent)["timing"] == {"('clk', 'rise')": 1.5, "/x.v": 2}
    assert json.loads(written) is None  # the run succeeded


def test_the_remote_runs_every_flow_clean_with_p2a_settings(tmp_path, monkeypatch):
    """The P2a floor guarantees a clean run in a directory named by its settings."""
    _, _, given, _ = _run_remote_runner(
        tmp_path, monkeypatch, dict(success=True), ["rebuild_all", "hashed_run_dirs", "clean"]
    )
    assert given == dict(
        backups=False, post_cleanup=False, display_results=False, hashed_run_dirs=True, clean=True
    )


def test_the_remote_runs_in_a_run_root_of_its_own(tmp_path, monkeypatch):
    """Not in the directory the design archive is unpacked into, which holds files the remote's
    xeda did not create and would refuse as a run root."""
    _, _, _, roots = _run_remote_runner(
        tmp_path, monkeypatch, dict(success=True), ["hashed_run_dirs"]
    )
    assert roots == [str(tmp_path / "xeda_run")]


SQRT_SETTINGS = ["fpga.part=xc7a12tcsg325-1", "clock.period=5.0"]


def _sqrt_design(root: Path) -> Path:
    root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", root)
    (root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl:\n  sources: [sqrt.vhdl]\n  top: sqrt\n  clock: {port: clk}\n"
    )
    return root / "sqrt.yaml"


def test_a_remote_run_delivers_outputs_to_but_never_onto_a_read_input(tmp_path, remote_host):
    """`--outputs-to` from the fetched artifacts, recorded beside the hashed mirror; a file a read
    setting names is never a destination, even with --overwrite-outputs -- found at delivery,
    the first time the artifact's name is known."""
    design = _sqrt_design(tmp_path / "design")
    got, root = tmp_path / "got", tmp_path / "local" / "xeda_run"
    first = RemoteRunner(root, outputs_to=got).run_remote(
        design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS
    )
    assert first["success"] and first["deliveries"]
    assert re.fullmatch(r"vivado_synth_[0-9a-f]{16}", Path(first["run_path"]).name), "hashed"
    delivered = Path(first["deliveries"][0]["to"])
    assert delivered.is_relative_to(got) and delivered.is_file()
    kept = delivered.read_bytes()
    again = RemoteRunner(root, outputs_to=got, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="an input of the run"):
        again.run_remote(
            design,
            "vivado_synth",
            host="somewhere",
            flow_settings=[*SQRT_SETTINGS, f"xdc_files={delivered}"],
        )
    assert delivered.read_bytes() == kept
    assert list((tmp_path / "started_in").iterdir()) == [], "the start directory stays empty"


def test_a_remote_run_never_delivers_into_a_directory_a_setting_reads(
    tmp_path, remote_host, monkeypatch
):
    """gpt-6-sol's PR 2 review, finding 1, remote: outputs delivered into a directory that a later
    run's `lib_paths` names are inputs of that run -- every file under it is -- so that run's
    `--outputs-to` there is refused before connecting, --overwrite-outputs or not, and xeda's
    earlier copies stay as they were."""
    design = _sqrt_design(tmp_path / "design")
    got, root = tmp_path / "got", tmp_path / "local" / "xeda_run"
    first = RemoteRunner(root, outputs_to=got).run_remote(
        design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS
    )
    assert first["success"] and first["deliveries"]
    kept = {p: p.read_bytes() for p in got.rglob("*") if p.is_file()}
    assert kept
    monkeypatch.setattr(remote_module, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(DeliveryError, match=rf"`lib_paths\[0\]\[1\]` names {got.resolve()}"):
        RemoteRunner(root, outputs_to=got, overwrite_outputs=True).run_remote(
            design,
            "vivado_synth",
            host="somewhere",
            flow_settings=[*SQRT_SETTINGS, {"lib_paths": [["work", str(got)]]}],
        )
    assert {p: p.read_bytes() for p in got.rglob("*") if p.is_file()} == kept


def test_a_remote_run_guards_the_directories_its_flow_reads_as_the_command_line_leaves_them(
    tmp_path, remote_host, monkeypatch
):
    """gpt-6-sol's re-check: the requested flow's own `[flows.<flow>]` section is registered as
    the launch uses it, the command line's settings over it -- `lib_paths` given on the command
    line replaces the section's, so the section's directory is no input and `--outputs-to` may
    deliver there -- while a destination inside the directory the run does read is refused."""
    design = _sqrt_design(tmp_path / "design")
    old, new = tmp_path / "old", tmp_path / "new"
    for directory in (old, new):
        directory.mkdir()
        (directory / "cells.v").write_text("module cell; endmodule\n")
    with design.open("a") as yaml_file:
        yaml_file.write(
            f"flows:\n  vivado_synth:\n    lib_paths: [[work, {json.dumps(str(old))}]]\n"
        )
    root = tmp_path / "local" / "xeda_run"
    overridden = [*SQRT_SETTINGS, {"lib_paths": [["work", str(new)]]}]
    result = RemoteRunner(root, outputs_to=old).run_remote(
        design, "vivado_synth", host="somewhere", flow_settings=overridden
    )
    assert result["success"] and result["deliveries"]
    assert all(Path(d["to"]).is_relative_to(old) for d in result["deliveries"])
    monkeypatch.setattr(remote_module, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(DeliveryError, match=rf"`lib_paths\[0\]\[1\]` names {new.resolve()}"):
        RemoteRunner(root, outputs_to=new / "got", overwrite_outputs=True).run_remote(
            design, "vivado_synth", host="somewhere", flow_settings=overridden
        )
    assert sorted(p.name for p in new.iterdir()) == ["cells.v"]


def test_a_remote_run_refuses_an_outputs_to_directory_before_connecting(
    tmp_path, remote_host, monkeypatch
):
    """As a local launch does (`Deliveries.check_outputs_to`): a run root, an input, or an
    existing non-directory file named by `--outputs-to` is refused up front, before the run's
    tools -- here, before the remote is even connected to. `remote_host` sets up the whole
    filesystem-backed transport; overriding `Connection` to fail if constructed proves the refusal
    happens before it, not merely before the run reports back."""
    design = _sqrt_design(tmp_path / "design")
    root = tmp_path / "local" / "xeda_run"
    monkeypatch.setattr(remote_module, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(DeliveryError, match="run root"):
        RemoteRunner(root, outputs_to=root / "grab").run_remote(
            design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS
        )


def test_a_remote_run_reports_partial_deliveries_before_an_oserror_re_raises(
    tmp_path, remote_host, monkeypatch
):
    """Mirrors the local fix (`_finish_launch`/`Deliveries.deliver`): `--outputs-to` fetches
    several files here (vivado_synth's reports and log), so an `OSError` from `_copy` on the
    second one must not lose the first from `results["deliveries"]` -- the dict `_deliver_fetched`
    builds for `--json` -- even though `Deliveries.deliver`'s own `finally` already wrote it to
    the on-disk delivery record. `Deliveries._copy` itself (not the shared `shutil.copyfileobj`,
    which the remote pipeline also uses to send/fetch the archive and artifacts) is the call to
    intercept, so only a delivery's own copies are counted."""
    import xeda.deliver

    design = _sqrt_design(tmp_path / "design")
    root = tmp_path / "local" / "xeda_run"
    real_copy = xeda.deliver.Deliveries._copy
    made: list = []

    def copy_then_fail_second(self, delivery, source, destination, expected, sha):
        made.append(destination)
        if len(made) == 2:
            raise OSError("disk full")
        return real_copy(self, delivery, source, destination, expected, sha)

    monkeypatch.setattr(xeda.deliver.Deliveries, "_copy", copy_then_fail_second)
    captured: dict = {}
    real_print_results = remote_module.print_results

    def capture_print_results(**kwargs):
        captured["results"] = kwargs["results"]
        return real_print_results(**kwargs)

    monkeypatch.setattr(remote_module, "print_results", capture_print_results)
    runner = RemoteRunner(root, outputs_to=tmp_path / "got")
    with pytest.raises(OSError, match="disk full"):
        runner.run_remote(design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS)
    results = captured["results"]
    assert results["success"]
    assert len(results["deliveries"]) == 1
    delivered = Path(results["deliveries"][0]["to"])
    assert delivered.is_relative_to(tmp_path / "got") and delivered.is_file()


def _locked_elsewhere(run_path: Path) -> bool:
    """Whether another holder has `run_path`'s run directory lock: a lock taken through a new
    open file is refused while any other open file holds it, in this process too."""
    import fcntl

    with open(lock_file(run_path), "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="no run directory lock on Windows")
def test_the_mirror_is_written_and_delivered_under_its_run_directory_lock(
    tmp_path, remote_host, monkeypatch
):
    """The mirror is the directory a local `--hashed-run-dirs` run of the same settings uses, and
    another remote run's: every write to it, and its delivery, holds that directory's lock, as a
    local launch does, so concurrent runs take turns instead of mixing their files."""
    design = _sqrt_design(tmp_path / "design")
    root = tmp_path / "local" / "xeda_run"
    writes: list = []

    def recorded(what, run_path_of, real):
        def call(*args, **kwargs):
            run_path = run_path_of(*args, **kwargs)
            writes.append((what, run_path, _locked_elsewhere(run_path)))
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(
        remote_module,
        "dump_json",
        recorded("dump_json", lambda data, path, **_: Path(path).parent, remote_module.dump_json),
    )
    monkeypatch.setattr(
        remote_module,
        "remove_trace",
        recorded("remove_trace", lambda run_path: run_path, remote_module.remove_trace),
    )
    monkeypatch.setattr(
        remote_module,
        "_transfer_artifacts",
        recorded(
            "_transfer_artifacts",
            lambda conn, artifacts, remote_run_path, local_dir, **kwargs: local_dir.parent,
            remote_module._transfer_artifacts,
        ),
    )
    monkeypatch.setattr(
        remote_module.Deliveries,
        "deliver",
        recorded("deliver", lambda self: self.run_path, remote_module.Deliveries.deliver),
    )
    results = RemoteRunner(root, outputs_to=tmp_path / "got").run_remote(
        design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS
    )

    assert results["success"] and results["deliveries"]
    mirror = Path(results["run_path"])
    assert {what for what, _, _ in writes} == {
        "dump_json",
        "remove_trace",
        "_transfer_artifacts",
        "deliver",
    }
    assert all(run_path == mirror for _, run_path, _ in writes), writes
    assert [what for what, _, locked in writes if not locked] == []
    assert not _locked_elsewhere(mirror), "the lock is released once the run is delivered"


def test_a_refused_delivery_still_closes_the_gateway_and_the_connection(
    tmp_path, remote_host, monkeypatch
):
    design = _sqrt_design(tmp_path / "design")
    closed: list = []
    real_makegateway = remote_module.execnet.makegateway

    def makegateway(spec):
        gateway = real_makegateway(spec)
        real_exit = gateway.exit

        def exit():
            closed.append("gateway")
            real_exit()

        gateway.exit = exit
        return gateway

    def refuse(self, *args, **kwargs):
        raise DeliveryError("refused")

    monkeypatch.setattr(remote_module.execnet, "makegateway", makegateway)
    monkeypatch.setattr(_LocalConnection, "close", lambda self: closed.append("connection"))
    monkeypatch.setattr(RemoteRunner, "_deliver_fetched", refuse)
    with pytest.raises(DeliveryError, match="refused"):
        RemoteRunner(tmp_path / "local" / "xeda_run", outputs_to=tmp_path / "got").run_remote(
            design, "vivado_synth", host="somewhere", flow_settings=SQRT_SETTINGS
        )
    assert sorted(closed) == ["connection", "gateway"]


def test_a_remote_run_builds_the_default_fpga_graph_and_mirrors_its_bitstream(
    tmp_path, remote_host, monkeypatch
):
    """`fpga_pack` on the remote: its producers run there in their own run directories, and the
    recorded bitstream comes back, verified, into the local mirror. Process fakes on both ends
    (no real synthesis, and nothing that programs)."""
    from xeda.digest import content_digest

    from .tool_utils import use_fake_fpga_tools

    use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    path = os.environ["PATH"]  # the fake toolchain first, as the remote's login shell has it
    monkeypatch.setattr(
        remote_module, "get_login_env", lambda conn: {"PATH": path, "HOME": str(remote_host)}
    )
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    settings = ["fpga.part=LFE5U-25F-6BG381C"]
    expected = runner.resolve(get_flow_class("fpga_pack"), design, settings)
    assert [node.name for node in expected.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack"]
    results = runner.run_remote(design, "fpga_pack", "fake", flow_settings=settings)
    assert results and results["success"]
    assert results["flow_hash"] == expected.node("fpga_pack").flowrun_hash
    recorded = results["outputs"]["bitstream"]
    mirrored = Path(recorded["path"])
    assert mirrored.is_relative_to(tmp_path / "mirror") and mirrored.name == "top.bit"
    assert mirrored.read_bytes() == b"\x00\xffXEDA bitstream\x00"
    assert content_digest(mirrored) == recorded["sha"]
    tools = {}
    for name in ("yosys_fpga", "nextpnr", "fpga_pack"):
        calls = (_remote_run_dir(remote_host, name) / "fake_fpga.calls.jsonl").read_text()
        tools[name] = [json.loads(line)["tool"] for line in calls.splitlines()]
    assert tools == {"yosys_fpga": ["yosys"], "nextpnr": ["nextpnr-ecp5"], "fpga_pack": ["ecppack"]}
    packed = _remote_run_dir(remote_host, "fpga_pack")
    trace = json.loads((packed / "trace.json").read_text())
    assert trace["declared_inputs"][0]["origin"] == expected.node("fpga_pack").inputs[0].origin


def test_a_remote_run_programs_a_prebuilt_bitstream_with_the_worker_s_fake_loader(
    tmp_path, remote_host, monkeypatch
):
    """`openfpgaloader` on the remote, given a typed bitstream source: the loader alone runs
    there, on the shipped file. NO REAL PROGRAMMER: the worker's PATH starts with the fake
    toolchain, `openFPGALoader` on it is checked to be the fake before anything is sent, and
    the call record only the fake writes is what proves which program ran."""
    from .tool_utils import FAKE_TOOLS_DIR, use_fake_fpga_tools

    prefix = use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    path = os.environ["PATH"]
    loader = shutil.which("openFPGALoader", path=path)
    assert loader == str(prefix / "bin/openFPGALoader")
    assert Path(loader).read_bytes() == (FAKE_TOOLS_DIR / "fake_fpga_tool.py").read_bytes()
    monkeypatch.setattr(
        remote_module, "get_login_env", lambda conn: {"PATH": path, "HOME": str(remote_host)}
    )
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    (root / "built.bit").write_bytes(b"a bitstream built elsewhere")
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v", {"file": "built.bit", "type": "Bitstream"}], "top": "top"},
    )
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    settings = ["board=ULX3S_85F"]
    expected = runner.resolve(get_flow_class("openfpgaloader"), design, settings)
    assert [node.name for node in expected.nodes] == ["openfpgaloader"]
    results = runner.run_remote(design, "openfpgaloader", "fake", flow_settings=settings)
    assert results and results["success"]
    remote_run = _remote_run_dir(remote_host, "openfpgaloader")
    assert [p.name.split("_")[0] for p in remote_run.parent.iterdir() if p.is_dir()] == [
        "openfpgaloader"
    ]
    (call,) = [
        json.loads(line) for line in (remote_run / "fake_fpga.calls.jsonl").read_text().splitlines()
    ]
    assert call["tool"] == "openFPGALoader"
    shipped = Path(call["argv"][1])
    assert call["argv"][0] == "--bitstream" and shipped.is_relative_to(remote_host)
    assert shipped.read_bytes() == b"a bitstream built elsewhere"
    assert call["argv"][2:] == ["--board", "ulx3s", "--fpga-part", "LFE5U-85F-6BG381C"]


def _nextpnr_design(tmp_path: Path) -> Path:
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = tmp_path / "d.yaml"
    design.write_text(
        "name: d\nrtl:\n  sources: [top.v]\n  top: top\n"
        "flows:\n  nextpnr:\n    fpga: {part: LFE5U-25F-6BG381C}\n"
    )
    return design


def test_a_remote_run_takes_dash_s_flows_node_key_as_a_local_run_does(tmp_path, monkeypatch):
    """`-s flows.<node>.key` reaches the remote as the requested flow's nested setting; a flow
    outside the run is refused before connecting."""
    connected_to = []

    class _Unreachable(_LocalConnection):
        def __init__(self, host, user=None, port=None):
            connected_to.append(host)
            raise ConnectionRefusedError(f"connected to {host}")

    monkeypatch.setattr(remote_module, "Connection", _Unreachable)
    monkeypatch.chdir(tmp_path)
    design = _nextpnr_design(tmp_path)
    runner = RemoteRunner(tmp_path / "xeda_run")

    with pytest.raises(FlowSettingsError, match=r"yosys_fpg.*yosys_fpga"):
        runner.run_remote(
            design, "nextpnr", host="h", flow_settings=["flows.yosys_fpg.flatten=true"]
        )
    assert not connected_to

    with pytest.raises(ConnectionRefusedError):
        runner.run_remote(
            design, "nextpnr", host="h", flow_settings=["flows.yosys_fpga.flatten=true"]
        )
    assert connected_to == ["h"]


def test_the_archive_names_every_source_s_type(tmp_path, monkeypatch):
    """A P2a remote reloads every new type, including Data on unknown and HDL suffixes."""
    root = tmp_path / "d"
    root.mkdir()
    members = list(SourceType)
    sources = []
    for member in members:
        name = f"{member.name.lower()}.src"
        (root / name).write_text("x\n")
        sources.append({"file": name, "type": member.name})
    for name in ("notes.txt", "data.v"):
        (root / name).write_text("x\n")
        sources.append({"file": name, "type": "Data"})
        members.append(SourceType.Data)
    design = Design(
        name="d", design_root=root, rtl={"sources": sources, "top": "t"}, tb={"sources": sources}
    )
    monkeypatch.setattr(remote_module, "Connection", _LocalConnection)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    zip_name, design_name = remote_module.send_design(
        design, _LocalConnection("somewhere"), str(remote_dir)
    )
    with zipfile.ZipFile(remote_dir / zip_name) as archive:
        shipped = json.loads(archive.read(design_name))
        archive.extractall(remote_dir)
    for part in ("rtl", "tb"):
        assert [source["type"] for source in shipped[part]["sources"]] == [m.name for m in members]
        assert all(
            set(source) <= {"file", "type", "standard", "variant"}
            for source in shipped[part]["sources"]
        )
    gw = execnet.makegateway(f"popen//python={sys.executable}")
    try:
        probe = gw.remote_exec(remote_module.REMOTE_PROBE).receive()
        remote_module.check_remote_xeda(probe[3], probe[4], probe[2], probe[5])
        restored = gw.remote_exec(
            "from xeda import Design\n"
            f"design = Design.from_file({str(remote_dir / design_name)!r})\n"
            "channel.send([[s.type.name for s in part.sources] for part in (design.rtl, design.tb)])\n"
        ).receive()
        assert restored == [[m.name for m in members]] * 2
    finally:
        gw.exit()


def _unreachable_remote(monkeypatch) -> list:
    connected_to: list = []

    class _Unreachable(_LocalConnection):
        def __init__(self, host, user=None, port=None):
            connected_to.append(host)
            raise ConnectionRefusedError(f"connected to {host}")

    monkeypatch.setattr(remote_module, "Connection", _Unreachable)
    return connected_to


def test_a_remote_run_refuses_a_directory_with_two_project_files_as_a_local_run_does(
    tmp_path, monkeypatch
):
    """One discovery rule for both: more than one project file is a `ProjectFileError` naming
    them, before anything connects."""
    connected_to = _unreachable_remote(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = _nextpnr_design(tmp_path)
    (tmp_path / PROJECT_FILE).write_text("flows: {}\n")
    (tmp_path / TOML_PROJECT_FILE).write_text("[flows]\n")

    with pytest.raises(ProjectFileError, match=rf"{PROJECT_FILE}.*{TOML_PROJECT_FILE}.*keep one"):
        RemoteRunner(tmp_path / "xeda_run").run_remote(design, "nextpnr", host="h")
    with pytest.raises(ProjectFileError, match=r"keep one"):
        RemoteRunner(tmp_path / "xeda_run").run_remote(Design(name="d"), "nextpnr", host="h")
    assert not connected_to


@pytest.mark.parametrize("given", ["missing.yaml", Path("missing.toml")])
def test_a_remote_run_reports_a_missing_project_file_as_a_local_run_does(
    tmp_path, monkeypatch, given
):
    connected_to = _unreachable_remote(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ProjectFileError, match=r"Cannot open project file .*missing.*no such file"):
        RemoteRunner(tmp_path / "xeda_run").run_remote(
            _nextpnr_design(tmp_path), "nextpnr", host="h", xedaproject=given
        )
    assert not connected_to


def test_a_remote_run_treats_an_empty_project_name_as_none_given(tmp_path, monkeypatch):
    connected_to = _unreachable_remote(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConnectionRefusedError):
        RemoteRunner(tmp_path / "xeda_run").run_remote(
            _nextpnr_design(tmp_path), "nextpnr", host="h", xedaproject=""
        )
    assert connected_to == ["h"]


@pytest.mark.parametrize("name", PROJECT_FILE_NAMES)
def test_a_remote_run_finds_a_lone_project_file_by_any_accepted_name(tmp_path, monkeypatch, name):
    """The project is found (a broken one is reported, not skipped), whichever of the accepted
    names it has; it does not have to be named `xedaproject.toml`."""
    connected_to = _unreachable_remote(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = _nextpnr_design(tmp_path)
    (tmp_path / name).write_text(
        "flows: {nextpnr: {flatten: 1, flatten: 2}}\n"
        if not name.endswith(".toml")
        else "[flows.nextpnr\nflatten = 1\n"
    )

    with pytest.raises(Exception, match=r"flatten|line 1"):
        RemoteRunner(tmp_path / "xeda_run").run_remote(design, "nextpnr", host="h")
    assert not connected_to

    (tmp_path / name).write_text("flows: {}\n" if not name.endswith(".toml") else "[flows]\n")
    with pytest.raises(ConnectionRefusedError):
        RemoteRunner(tmp_path / "xeda_run").run_remote(design, "nextpnr", host="h")
    assert connected_to == ["h"]


@pytest.mark.parametrize("flow_name", ["_taker", "ghdl_sim"])
@pytest.mark.parametrize("origin", ["design", "command_line", "command_line_flow_section"])
def test_remote_input_bindings_are_refused_before_connecting(
    tmp_path, monkeypatch, origin, flow_name
):
    from .io_flows import _Taker

    flow_name = _Taker.name if flow_name == "_taker" else flow_name
    connected = []
    monkeypatch.setattr(remote_module, "Connection", lambda **kwargs: connected.append(kwargs))
    binding = "__maker.made"
    flow = {flow_name: {"inputs": {"made": binding}}} if origin == "design" else {}
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"}, flow=flow)
    settings = {
        "design": None,
        "command_line": {"inputs.made": binding},
        "command_line_flow_section": {f"flows.{flow_name}.inputs.made": binding},
    }[origin]
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(
            design, flow_name, "fake", flow_settings=settings
        )
    assert not connected and not (tmp_path / "mirror").exists()


def test_a_saved_binding_the_remote_request_does_not_reach_is_no_refusal(tmp_path, monkeypatch):
    """M2: as `dse` does, `--remote` refuses a binding its request reaches, not a design that
    merely saves one for another flow."""
    from .io_flows import _Maker, _Taker

    class _Connecting(Exception):
        pass

    def connect(**kwargs):
        raise _Connecting

    monkeypatch.setattr(remote_module, "Connection", connect)
    flows = {_Taker.name: {"inputs": {"made": "__input_maker.made"}}}
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"}, flow=flows)
    with pytest.raises(_Connecting):
        RemoteRunner(tmp_path / "mirror").run_remote(design, _Maker.name, "fake")
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "other").run_remote(design, _Taker.name, "fake")


def test_the_mirror_of_a_declared_flow_is_named_by_its_plan_identity(tmp_path, monkeypatch):
    """I3: two configurations of a producer are two mirrors of the consumer, each named by
    the requested node's identity in the plan, which is what the remote's `flow_hash` is."""
    from .io_flows import _Taker

    class _Named(Exception):
        pass

    def named(self, design_name, flow_name, identity):
        raise _Named(identity)

    monkeypatch.setattr(RemoteRunner, "get_flow_run_path", named)
    seen = []
    for text in ("one\n", "two\n"):
        design = Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": [], "top": "t"},
            flow={"__maker": {"text": text}},
        )
        runner = RemoteRunner(tmp_path / "mirror")
        planned = runner.resolve(_Taker, design, {}, design.flow).node(_Taker.name)
        with pytest.raises(_Named) as named_as:
            runner.run_remote(design, _Taker.name, "fake")
        assert str(named_as.value) == planned.flowrun_hash
        seen.append(planned.flowrun_hash)
    assert seen[0] != seen[1]


# ------------------------------------------- O-RI1 (b): a bundled platform on another install


def _remote_runner_installed_elsewhere(channel, **kwargs):
    """The worker of a remote whose xeda is installed at another prefix: its bundled platforms,
    and its own package directory, are a copy under the remote's run directory. execnet ships
    this function's source alone, so it imports what it uses."""
    import shutil
    from pathlib import Path

    import xeda
    import xeda.platforms.platform
    import xeda.utils
    from xeda.flow_runner.remote import remote_runner

    package = Path(kwargs["remote_path"]).parent / "elsewhere" / "site-packages" / "xeda"
    if not package.exists():
        shutil.copytree(
            Path(xeda.__file__).parent / "platforms" / "nangate45",
            package / "platforms" / "nangate45",
        )
    xeda.utils.XEDA_PACKAGE_ROOT = package
    xeda.platforms.platform.files = {"xeda.platforms": package / "platforms"}.get
    remote_runner(channel, **kwargs)


@pytest.mark.parametrize("elsewhere", [False, True], ids=["same install", "another install"])
def test_a_bundled_platform_s_remote_run_has_this_side_s_identity(
    tmp_path, remote_host, monkeypatch, elsewhere
):
    """O-RI1 (b), PCD23: `--remote yosys -s platform=nangate45` is accepted by a remote whose
    xeda is installed under another prefix, as every remote on another machine is: the remote's
    `flow_hash` equals this side's, and the run maps to Nangate45 cells. (It used to be refused
    even from the same installation: the platform's per-corner liberty files reached the remote
    as unexpanded `$DESIGN_ROOT/...` text, which named no shipped file.)"""
    require_yosys()
    if elsewhere:
        monkeypatch.setattr(remote_module, "remote_runner", _remote_runner_installed_elsewhere)
    root = tmp_path / "design"
    root.mkdir()
    (root / "mac.v").write_text(
        "module mac(input clk, input [7:0] a, b, output reg [15:0] q);\n"
        "  always @(posedge clk) q <= a * b;\nendmodule\n"
    )
    design = Design(
        name="mac",
        design_root=root,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
    )
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    settings = ["platform=nangate45", "clock.period=2.0"]
    expected = runner.plan("yosys", design, flow_settings=settings).node("yosys").flowrun_hash
    results = runner.run_remote(design, "yosys", "fake", flow_settings=settings)
    assert results and results["success"], results
    assert results["flow_hash"] == expected
    # the remote reads the platform it is sent (relocated under its run directory), whatever
    # its own installation holds; its identity names neither place
    remote_settings = json.loads(
        (_remote_run_dir(remote_host, "yosys") / "settings.json").read_text()
    )
    assert Path(remote_settings["flow_settings"]["platform"]["root_dir"]).is_relative_to(
        remote_host
    )
    netlist = Path(results["outputs"]["netlist"]["path"]).read_text()
    assert "_X1 " in netlist or "_X2 " in netlist
