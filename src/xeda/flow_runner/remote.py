import json
import logging
import os
import socket
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, IO, Optional, Tuple, Union

import execnet
from fabric import Connection
from fabric.transfer import Transfer

from ..design import Design, DesignSource
from ..utils import XedaException, hierarchical_merge, dump_json, settings_to_dict, semantic_hash
from ..version import __version__
from .default_runner import FlowLauncher, print_results

log = logging.getLogger(__name__)


def send_design(design: Design, conn, remote_path: str) -> Tuple[str, str]:
    assert isinstance(conn, Connection)

    def uniquify_filename(src: DesignSource) -> str:
        return src.file.stem + f"_{src.content_hash[:8]}" + src.file.suffix

    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_dir = Path(tmpdirname)
        zip_file = temp_dir / f"{design.name}.zip"
        log.info("Preparing design archive: %s", zip_file)
        new_design: Dict[str, Any] = {**design.dict(), "design_root": None}
        rtl: Dict[str, Any] = {}
        tb: Dict[str, Any] = {}
        remote_sources_path = Path(design.name) / "sources"
        rtl_sources: Dict[DesignSource, str] = {}
        for src in design.rtl.sources:
            filename = src.file.name
            if filename in rtl_sources:
                filename = uniquify_filename(src)
            rtl_sources[src] = filename
        rtl["sources"] = [remote_sources_path / s for s in rtl_sources.values()]
        rtl["defines"] = design.rtl.defines
        rtl["attributes"] = design.rtl.attributes
        rtl["parameters"] = design.rtl.parameters
        rtl["top"] = design.rtl.top
        rtl["clocks"] = [clk.dict() for clk in design.rtl.clocks]
        # FIXME add src type/attributes
        tb_sources: Dict[DesignSource, str] = {}
        for src in design.tb.sources:
            filename = src.file.name
            if filename in tb_sources:
                filename = uniquify_filename(src)
            tb_sources[src] = filename
        tb["sources"] = [remote_sources_path / s for s in tb_sources.values()]
        tb["top"] = design.tb.top
        tb["cocotb"] = design.tb.cocotb
        if design.tb.uut:
            tb["uut"] = design.tb.uut
        if design.tb.parameters:
            tb["parameters"] = design.tb.parameters
        if design.tb.defines:
            tb["defines"] = design.tb.defines
        new_design["rtl"] = rtl
        new_design["tb"] = tb
        new_design["flow"] = design.flow
        design_file = temp_dir / f"{design.name}.xeda.json"
        with open(design_file, "w") as f:
            json.dump(
                new_design,
                f,
                default=lambda obj: (
                    obj.json
                    if hasattr(obj, "json")
                    else (
                        obj.__json_encoder__
                        if hasattr(obj, "__json_encoder__")
                        else obj.__dict__ if hasattr(obj, "__dict__") else str(obj)
                    )
                ),
            )
        all_sources = rtl_sources
        all_sources.update(tb_sources)
        with zipfile.ZipFile(zip_file, mode="w") as archive:
            for src, server_path in all_sources.items():
                archive.write(src.path, arcname=remote_sources_path / server_path)
            archive.write(design_file, arcname=design_file.relative_to(temp_dir))

        with zipfile.ZipFile(zip_file, mode="r") as archive:
            archive.printdir()

        log.info("Transfering design to %s in %s", conn.host, remote_path)
        conn.put(zip_file, remote=remote_path)
        return zip_file.name, design_file.name


def remote_runner(channel, remote_path, zip_file, flow, design_file, flow_settings, env=None):
    # pylint: disable=import-outside-toplevel,reimported,redefined-outer-name
    import json
    import os
    import zipfile
    from pathlib import Path

    try:
        from xeda.flow_runner import DefaultRunner
    except ImportError:
        # FIXME: we need to be able to create/use virtual env or change our remote approach
        raise Exception("XEDA Python package not found. Please install it using pip.")

    os.chdir(remote_path)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    if channel.isclosed():
        return

    with zipfile.ZipFile(zip_file, mode="r") as archive:
        archive.extractall(path=remote_path)

    # xeda_run_dir = Path("remote_run").joinpath(datetime.now().strftime("%y%m%d%H%M%S%f"))
    xeda_run_dir = str(Path.cwd())
    launcher = DefaultRunner(
        xeda_run_dir,
        cached_dependencies=True,
        backups=False,
        cleanup_before_run=True,
        incremental=False,
        post_cleanup=False,
        display_results=False,
    )
    # NOTE: this function's *source* is shipped to the remote host and executed
    # there against whatever xeda version is installed remotely, so it must not
    # depend on APIs newer than that install. Live output streaming is set up
    # separately, at the file-descriptor level, by `STREAM_OUTPUT_SETUP` (pure
    # stdlib, no xeda involvement) -- see `RemoteRunner.run_remote`.
    f = launcher.run(
        flow,
        design=design_file,
        design_allow_extra=True,
        flow_settings=flow_settings,
    )
    results = json.dumps(
        {"success": False} if f is None else f.results.to_dict(),
        default=str,
        indent=1,
    )
    channel.send(results)


def get_env_var(conn, var):
    result = conn.run(f"echo ${var}", hide=True, pty=False)
    assert result.ok
    return result.stdout.strip()


def get_login_env(conn: Connection) -> Dict[str, str]:
    try:
        result = conn.run("$SHELL -l -c env", hide=True, pty=False)
    except socket.gaierror as e:
        raise XedaException(f"Error connecting to {conn.host}: {e}")

    assert result.ok

    lines_split = [line.split("=") for line in result.stdout.strip().split("\n")]
    return {kv[0]: kv[1] for kv in lines_split if len(kv) == 2}


class RemoteLogger:
    """Echoes data received from a remote output-forwarding execnet channel to a
    local stream, live, as each message arrives."""

    def __init__(self, stream: IO[str], label: str = "remote"):
        self.stream = stream
        self.label = label

    def cb(self, data: Optional[str]) -> None:
        if data is None:
            log.debug("Remote %s channel closed.", self.label)
            return
        self.stream.write(data)
        self.stream.flush()


# Executed on the remote host, in the gateway (worker) process, to stream the
# flow's output back live.
#
# It takes over the worker's stdout/stderr *file descriptors* (1 and 2) rather
# than the Python-level `sys.stdout`/`sys.stderr` objects, because that is what
# a spawned EDA tool actually inherits. execnet's own bootstrap
# (`execnet.gateway_base.init_popen_io`) has already `dup`ed the real fd 1 aside
# for its wire protocol and pointed fd 1 at /dev/null, so fds 1/2 are free for
# us to claim -- and anything the flow writes to them, directly or from any
# subprocess it spawns, then lands in our pty and is forwarded here line by
# line as it is produced.
#
# IMPORTANT: this runs against whatever xeda happens to be installed on the
# remote host, which may be older than the local one. It therefore uses only
# the standard library and must stay free of any xeda import, so that live
# output streaming never depends on the remote xeda version.
STREAM_OUTPUT_SETUP = r"""
import os
import sys
import threading


def _open_stream_pair():
    # A pty (rather than a pipe) so that tools which only line-buffer when
    # attached to a terminal keep doing so, and their output arrives here
    # incrementally instead of in big blocks. Returns (read_fd, write_fd).
    try:
        import pty
        import termios
    except ImportError:  # non-POSIX remote: fall back to a plain pipe
        return os.pipe()
    read_fd, write_fd = pty.openpty()
    try:
        # Turn off output post-processing entirely, so the pty cannot rewrite
        # what the tool wrote (by default it would at least turn every '\n'
        # into '\r\n'). We want it to look like a terminal purely so tools keep
        # line-buffering -- the bytes themselves must come through untouched,
        # so what you see locally is what the tool actually printed.
        attrs = termios.tcgetattr(write_fd)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(write_fd, termios.TCSANOW, attrs)
    except Exception:
        pass  # the pump drops any redundant CR that slips through anyway
    return read_fd, write_fd


def _pump(read_fd, out_channel):
    # A carriage return immediately next to a newline carries no information:
    # after a '\n' the cursor is already at the start of a line. Drop those, so
    # each line of remote output renders as exactly one line here.
    #
    # This has to happen on this side of the wire rather than in the remote's
    # xeda: the remote may be running an older xeda whose `run_process` prints
    # each already-newline-terminated line with `end="\r"`, emitting "\n\r" per
    # line -- which is what made every line of tool output come out with a
    # blank line after it. We cannot patch the xeda installed over there, but
    # this code is shipped from here, so it works against any remote version.
    #
    # A LONE '\r' is left untouched: tools use it to redraw a progress line in
    # place, and dropping it would turn a progress bar into a wall of lines.
    after_newline = False
    while True:
        try:
            data = os.read(read_fd, 4096)
        except OSError:
            break  # EIO: last writer of the pty slave closed == EOF
        if not data:
            break
        text = data.decode("utf-8", "replace")
        # ...also when the '\n' ended the previous chunk and the '\r' starts
        # this one. Only one flag of state is needed, so no output is ever held
        # back waiting for more input, and streaming stays live.
        if after_newline:
            text = text.lstrip("\r")
        after_newline = text.endswith("\n")
        text = text.replace("\n\r", "\n").replace("\r\n", "\n")
        if text:
            out_channel.send(text)
    out_channel.close()


outchan = channel.gateway.newchannel()
errchan = channel.gateway.newchannel()

out_r, out_w = _open_stream_pair()
err_r, err_w = _open_stream_pair()

# keep the worker's original fds so they can be restored on teardown
saved_out, saved_err = os.dup(1), os.dup(2)

sys.stdout.flush()
sys.stderr.flush()
os.dup2(out_w, 1)
os.dup2(err_w, 2)
os.close(out_w)
os.close(err_w)

pumps = [
    threading.Thread(target=_pump, args=(out_r, outchan), daemon=True),
    threading.Thread(target=_pump, args=(err_r, errchan), daemon=True),
]
for pump in pumps:
    pump.start()

channel.send((outchan, errchan))

channel.receive()  # blocks until the local side signals the run is over

# Restore the original fds. That drops the last writer of each pty slave, so
# the pumps see EOF and drain whatever is still buffered before exiting.
sys.stdout.flush()
sys.stderr.flush()
os.dup2(saved_out, 1)
os.dup2(saved_err, 2)
os.close(saved_out)
os.close(saved_err)
for pump in pumps:
    pump.join(10)
os.close(out_r)
os.close(err_r)

channel.send("stopped")  # local side waits for this, so no output is lost
"""


class RemoteRunner(FlowLauncher):
    class Settings(FlowLauncher.Settings):
        clean: bool = True
        backups: bool = False

    def run_remote(
        self,
        design: Union[str, Path, Design],
        flow_name: str,
        host: str,
        user: Optional[str] = None,
        port: Optional[int] = None,
        flow_settings=None,
    ):
        if isinstance(design, (str, Path)):
            design = Design.from_file(design)
        flow_settings = settings_to_dict(flow_settings or [])
        design_flow_settings = design.flow.pop(flow_name, {})
        if design_flow_settings:
            flow_settings = hierarchical_merge(flow_settings, design_flow_settings)
        flowrun_hash = semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                # copied_resources=[FileResource(res) for res in copy_resources],
            ),
        )
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )

        host_split = host.split(":")
        if port is None and len(host_split) == 2 and host_split[1].isnumeric():
            host = host_split[0]
            port = int(host_split[1])
        log.info(
            "Connecting to %s%s%s...", f"{user}@" if user else "", host, f":{port}" if port else ""
        )
        conn = Connection(host=host, user=user, port=port)
        log.info("logging in...")
        remote_env = get_login_env(conn)
        remote_env_path = remote_env.get("PATH", "")
        remote_home = remote_env.get("HOME", ".")
        log.info("Remote PATH=%s HOME=%s", remote_env_path, remote_home)

        remote_xeda = Path(remote_home) / ".xeda"
        if not Transfer(conn).is_remote_dir(str(remote_xeda)):

            conn.sftp().mkdir(str(remote_xeda))
        remote_xeda_run = remote_xeda / "remote_run"
        if not Transfer(conn).is_remote_dir(str(remote_xeda_run)):
            conn.sftp().mkdir(str(remote_xeda_run))
        # use a timestamped subdirectory to avoid any race conditions and also have the chronology clear
        remote_path = str(remote_xeda_run / datetime.now().strftime("%y%m%d%H%M%S%f"))
        if not Transfer(conn).is_remote_dir(remote_path):
            conn.sftp().mkdir(remote_path)
        assert Transfer(conn).is_remote_dir(remote_path)
        conn.sftp().chdir(remote_path)
        zip_file, design_file = send_design(design, conn, remote_path)

        ssh_opt = f"{host}"
        if user:
            ssh_opt = f"{user}@{ssh_opt}"
        if port:
            ssh_opt += f" -p {port}"

        python_exec = "python3"

        spec = {
            "ssh": ssh_opt,
            "chdir": remote_path,
            "env:PATH": remote_env_path,
            "python": python_exec,
        }
        gw = execnet.makegateway("//".join([f"{k}={v}" for k, v in spec.items()]))
        channel = gw.remote_exec("""
            import sys, os
            channel.send((sys.platform, tuple(sys.version_info), os.getpid()))
        """)
        platform, version_info, _ = channel.receive()
        version_info_str = ".".join(str(v) for v in version_info)
        log.info("Remote host:%s (%s python:%s)", host, platform, version_info_str)
        PY_MIN_VERSION = (3, 8, 0)
        assert version_info[0] == PY_MIN_VERSION[0] and (
            version_info[1] > PY_MIN_VERSION[1]
            or (version_info[1] == PY_MIN_VERSION[1] and version_info[2] >= PY_MIN_VERSION[2])
        ), f"Python {'.'.join(str(d) for d in PY_MIN_VERSION)} or newer is required to be installed on the remote but found version {version_info_str}"

        run_path = self.get_flow_run_path(
            design.name,
            flow_name,
            design_hash,
            flowrun_hash,
        )
        run_path.mkdir(parents=True, exist_ok=True)

        settings_json = run_path / "settings.json"
        results_json_path = run_path / "results.json"

        log.info("dumping effective settings to %s", settings_json)
        all_settings = dict(
            design=design,
            design_hash=design_hash,
            rtl_fingerprint=design.rtl_fingerprint,
            rtl_hash=design.rtl_hash,
            flow_name=flow_name,
            flow_settings=flow_settings,
            xeda_version=__version__,
            flowrun_hash=flowrun_hash,
        )
        dump_json(all_settings, settings_json, backup=self.settings.backups)
        results = None

        # Stream the remote flow's output (its own, and that of every tool it
        # spawns) back to this terminal live, line by line, as it is produced.
        stream_channel = gw.remote_exec(STREAM_OUTPUT_SETUP)
        outchan, errchan = stream_channel.receive()
        outchan.setcallback(RemoteLogger(sys.stdout, label="stdout").cb, endmarker=None)
        errchan.setcallback(RemoteLogger(sys.stderr, label="stderr").cb, endmarker=None)

        try:
            results_channel = gw.remote_exec(
                remote_runner,
                remote_path=remote_path,
                zip_file=zip_file,
                flow=flow_name,
                design_file=design_file,
                flow_settings=flow_settings,
            )
            if not results_channel.isclosed():
                results_str = results_channel.receive()
                if results_str:
                    results = json.loads(results_str)
            results_channel.waitclose()
        except execnet.gateway_base.RemoteError as e:
            log.critical("Remote exception: %s", e.formatted)
        finally:
            # Tear the redirection down and wait for the remote pumps to drain,
            # so trailing output can't be lost -- and so it can't land in the
            # middle of the results table printed below. Best-effort: if the
            # remote died, teardown will fail too, and that must not mask the
            # actual failure.
            try:
                stream_channel.send("stop")
                stream_channel.receive()
                stream_channel.waitclose()
            except (EOFError, OSError, execnet.gateway_base.RemoteError) as e:
                log.debug("Could not cleanly stop remote output streaming: %s", e)

        if results:
            print_results(
                results=results,
                title=f"Results of flow:{flow_name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

            artifacts = results.get("artifacts")
            artifacts_orig = artifacts
            remote_run_path = results.get("run_path")

            local_artifacts_dir = run_path / "artifacts"

            if remote_run_path and artifacts:
                assert isinstance(remote_run_path, str)
                if isinstance(artifacts, (dict)):
                    artifacts = list(artifacts.values())
                local_artifacts_dir.mkdir(exist_ok=True, parents=True)
                num_transferred = 0
                # remote path -> local path, used to rewrite `results["artifacts"]` below
                remote_to_local: Dict[str, str] = {}

                # TODO: compress artifacts in a single Zip file before transfer, unzip after transfer
                # conn.sftp().chdir(remote_run_path)
                # artifacts_zipfile = run_path / "artifacts.zip"
                # with zipfile.ZipFile(artifacts_zipfile, mode="w") as archive:
                #     for f in artifacts:
                #         remote_path = f if os.path.isabs(f) else remote_run_path + "/" + f
                #         rel_path = os.path.relpath(f, remote_run_path) if os.path.isabs(f) else f
                #         archive.write(remote_path, arcname=rel_path)
                # log.info("Transferring artifacts to %s", local_artifacts_dir.relative_to(Path.cwd()))
                # conn.get(str(artifacts_zipfile), str(local_artifacts_dir / "artifacts.zip"))
                # with zipfile.ZipFile(artifacts_zipfile, mode="r") as archive:
                #     archive.extractall(path=local_artifacts_dir)
                # artifacts_zipfile.unlink()

                for f in artifacts:
                    remote_path = f if os.path.isabs(f) else remote_run_path + "/" + f
                    rel_path = os.path.relpath(f, remote_run_path) if os.path.isabs(f) else f
                    local_path = local_artifacts_dir / rel_path
                    if local_path.exists():
                        # backup = backup_existing(local_path)
                        # log.warning("Backed up exitsting artifact to %s", str(backup))
                        pass
                    elif not local_path.parent.exists():
                        local_path.parent.mkdir(parents=True)
                    assert local_path.is_relative_to(local_artifacts_dir)
                    result = conn.get(remote_path, str(local_path))
                    log.debug("Transferred artifact %s to %s", f, result.local)
                    remote_to_local[f] = str(local_path)
                    num_transferred += 1
                if num_transferred > 0:
                    log.info(
                        "Transferred %d artifact(s) to %s",
                        num_transferred,
                        local_artifacts_dir.relative_to(Path.cwd()),
                    )

                # rewrite reported artifact paths to point at the local copies, preserving
                # the original list/dict shape
                if isinstance(artifacts_orig, dict):
                    results["artifacts"] = {
                        k: remote_to_local.get(v, v) if isinstance(v, str) else v
                        for k, v in artifacts_orig.items()
                    }
                else:
                    results["artifacts"] = [
                        remote_to_local.get(f, f) if isinstance(f, str) else f
                        for f in artifacts_orig
                    ]

            # the remote run_path no longer exists locally; report the local copy's path instead
            if "run_path" in results:
                results["run_path"] = str(run_path)

            dump_json(results, results_json_path, backup=True)
            log.info("Results written to %s", results_json_path)
        gw.exit()
        conn.close()
        return results
