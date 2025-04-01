from datetime import datetime
import json
import logging
import os
import socket
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import execnet
from fabric import Connection
from fabric.transfer import Transfer

from ..design import Design, DesignSource
from ..utils import backup_existing, dump_json, settings_to_dict, XedaException
from ..version import __version__
from .default_runner import (
    DIR_NAME_HASH_LEN,
    FlowLauncher,
    print_results,
    semantic_hash,
)

log = logging.getLogger(__name__)


def send_design(design: Design, conn, remote_path: str) -> Tuple[str, str]:
    root_path = design.root_path

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
    import os
    import zipfile
    import json
    from pathlib import Path

    from xeda.flow_runner import DefaultRunner

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

    def cb(self, data):
        if data is None:
            log.info("Remote channel closed.")
            return
        print(data, end="")


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
        # imports deferred due to "import imp" deprecation warnings from 'fabric'

        flow_settings = settings_to_dict(flow_settings or [])

        host_split = host.split(":")
        if port is None and len(host_split) == 2 and host_split[1].isnumeric():
            host = host_split[0]
            port = int(host_split[1])
        if isinstance(design, (str, Path)):
            design = Design.from_file(design)
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )
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

        flowrun_hash = semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                # copied_resources=[FileResource(res) for res in copy_resources],
                # xeda_version=__version__,
            ),
        )

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
        channel = gw.remote_exec(
            """
            import sys, os
            channel.send((sys.platform, tuple(sys.version_info), os.getpid()))
        """
        )
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

        receiver = RemoteLogger()

        outchan = gw.remote_exec(
            """
            import sys
            outchan = channel.gateway.newchannel()
            sys.stderr = sys.stdout = outchan.makefile("w")
            channel.send(outchan)
        """
        ).receive()
        outchan.setcallback(receiver.cb, endmarker=None)

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

        if results:
            print_results(
                results=results,
                title=f"Results of flow:{flow_name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

            artifacts = results.get("artifacts")
            remote_run_path = results.get("run_path")

            local_artifacts_dir = run_path / "artifacts"

            if remote_run_path and artifacts:
                assert isinstance(remote_run_path, str)
                if isinstance(artifacts, (dict)):
                    artifacts = list(artifacts.values())
                local_artifacts_dir.mkdir(exist_ok=True, parents=True)
                num_transferred = 0

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
                        backup = backup_existing(local_path)
                        log.warning("Backed up exitsting artifact to %s", str(backup))
                    elif not local_path.parent.exists():
                        local_path.parent.mkdir(parents=True)
                    assert local_path.is_relative_to(local_artifacts_dir)
                    result = conn.get(remote_path, str(local_path))
                    log.debug("Transferred artifact %s to %s", f, result.local)
                    num_transferred += 1
                if num_transferred > 0:
                    log.info(
                        "Transferred %d artifact(s) to %s",
                        num_transferred,
                        local_artifacts_dir.relative_to(Path.cwd()),
                    )

            dump_json(results, results_json_path, backup=True)
            log.info("Results written to %s", results_json_path)
        gw.exit()
        conn.close()
        return results
