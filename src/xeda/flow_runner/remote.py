import json
import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from ..design import Design
from ..utils import backup_existing, dump_json, settings_to_dict
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

    from fabric import Connection

    assert isinstance(conn, Connection)

    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_dir = Path(tmpdirname)
        zip_file = temp_dir / f"{design.name}.zip"
        log.info("Preparing design archive: %s", zip_file)
        new_design: Dict[str, Any] = {**design.dict(), "design_root": None}
        rtl: Dict[str, Any] = {}  # new_design.get("rtl", {})
        tb: Dict[str, Any] = {}  # new_design.get("tb", {})
        remote_sources_path = Path("sources")
        rtl["sources"] = [
            str(remote_sources_path / src.path.relative_to(root_path)) for src in design.rtl.sources
        ]
        rtl["top"] = design.rtl.top
        rtl["clocks"] = list(map(lambda kv: (kv[0], kv[1].dict()), design.rtl.clocks.items()))
        tb["sources"] = [
            str(remote_sources_path / src.file.relative_to(root_path)) for src in design.tb.sources
        ]
        tb["top"] = design.tb.top
        new_design["rtl"] = rtl
        new_design["tb"] = tb
        new_design["flow"] = design.flow
        design_file = temp_dir / f"{design.name}.xeda.json"
        with open(design_file, "w") as f:
            json.dump(new_design, f)
        with zipfile.ZipFile(zip_file, mode="w") as archive:
            for src in design.sources_of_type("*", rtl=True, tb=True):
                file_path = src.path
                archive.write(
                    file_path, arcname=remote_sources_path / file_path.relative_to(root_path)
                )
            archive.write(design_file, arcname=design_file.relative_to(temp_dir))

        with zipfile.ZipFile(zip_file, mode="r") as archive:
            archive.printdir()

        log.info("Transfering design to %s in %s", conn.host, remote_path)
        conn.put(zip_file, remote=remote_path)
        return zip_file.name, design_file.name


def remote_runner(channel, remote_path, zip_file, flow, design_file, flow_settings={}, env=None):
    import os
    import zipfile
    import json

    from xeda.flow_runner import DefaultRunner  # pyright: ignore reportMissingImports

    print(f"changing directory to {remote_path}")

    os.chdir(remote_path)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    if channel.isclosed():
        return

    with zipfile.ZipFile(zip_file, mode="r") as archive:
        archive.extractall(path=remote_path)

    xeda_run_dir = "remote_run"
    launcher = DefaultRunner(
        xeda_run_dir,
        cached_dependencies=True,
        backups=True,
        clean=True,
        incremental=False,
        post_cleanup=False,
        # post_cleanup_purge = True,
    )

    f = launcher.run(
        flow,
        design=design_file,
        flow_settings=flow_settings,
    )

    results = (
        "{success: false}"
        if f is None
        else json.dumps(
            f.results.to_dict(),
            default=str,
            indent=1,
        )
    )
    channel.send(results)


def get_env_var(conn, var):
    result = conn.run(f"echo ${var}", hide=True, pty=False)
    assert result.ok
    return result.stdout.strip()


def get_login_env(conn) -> Dict[str, str]:
    result = conn.run("$SHELL -l -c env", hide=True, pty=False)
    assert result.ok
    lines_split = [line.split("=") for line in result.stdout.strip().split("\n")]
    return {kv[0]: kv[1] for kv in lines_split if len(kv) == 2}


class RemoteRunner(FlowLauncher):
    class Settings(FlowLauncher.Settings):
        clean: bool = True

    def run_remote(
        self,
        design: Union[str, Path, Design],
        flow_name: str,
        host: str,
        user: Optional[str] = None,
        port: Optional[int] = None,
        flow_settings=[],
    ):
        flow_settings = settings_to_dict(flow_settings)
        # imports deferred due to "import imp" deprecation warnings from 'fabric'
        import execnet
        from fabric import Connection
        from fabric.transfer import Transfer

        if not isinstance(design, Design):
            design = Design.from_file(design)
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )
        conn = Connection(host=host, user=user, port=port)
        log.info(
            "Connecting to %s%s%s...", f"{user}@" if user else "", host, f":{port}" if port else ""
        )

        remote_env = get_login_env(conn)
        remote_env_path = remote_env.get("PATH", "")
        remote_home = remote_env.get("HOME")
        log.info("remote PATH=%s HOME=%s", remote_env_path, remote_home)

        remote_xeda = f"{remote_home}/.xeda"
        if not Transfer(conn).is_remote_dir(remote_xeda):
            conn.sftp().mkdir(remote_xeda)
        remote_path = f"{remote_xeda}/{design.name}_{design_hash[:DIR_NAME_HASH_LEN]}"
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
            ssh_opt += f"-p {port}"

        python_exec = "python3"

        gw = execnet.makegateway(
            f"ssh={ssh_opt}//chdir={remote_path}//env:PATH={remote_env_path}//python={python_exec}"
        )
        channel = gw.remote_exec(
            """
            import sys, os
            channel.send((sys.platform, tuple(sys.version_info), os.getpid()))
        """
        )
        platform, version_info, _ = channel.receive()
        version_info_str = ".".join(str(v) for v in version_info)
        log.info(
            f"Hostname:{host} Platform:{platform} Python:{version_info_str} PATH={remote_env_path}"
        )
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

            artifacts_dir = run_path / "artifacts"

            if remote_run_path and artifacts:
                assert isinstance(remote_run_path, str)
                if isinstance(artifacts, (dict)):
                    artifacts = list(artifacts.values())
                artifacts_dir.mkdir(exist_ok=True, parents=True)
                num_transferred = 0
                for f in artifacts:
                    remote_path = f if os.path.isabs(f) else remote_run_path + "/" + f
                    rel_path = os.path.relpath(f, remote_run_path) if os.path.isabs(f) else f
                    local_path = artifacts_dir / rel_path
                    if local_path.exists():
                        backup = backup_existing(local_path)
                        log.warning("Backed up exitsting artifact to %s", str(backup))
                    elif not local_path.parent.exists():
                        local_path.parent.mkdir(parents=True)
                    assert local_path.is_relative_to(artifacts_dir)
                    result = conn.get(remote_path, str(local_path))
                    log.debug("Transferred artifact %s to %s", f, result.local)
                    num_transferred += 1
                if num_transferred > 0:
                    log.info("Transferred %d artifact(s) to %s", num_transferred, artifacts_dir)

            dump_json(results, results_json_path, backup=True)
            log.info("Results written to %s", results_json_path)
        gw.exit()
        conn.close()
        return results
