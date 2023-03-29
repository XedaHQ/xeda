import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from fabric import Connection
from fabric.transfer import Transfer

from ..design import Design
from ..utils import dump_json
from ..version import __version__
from .default_runner import (
    DIR_NAME_HASH_LEN,
    FlowLauncher,
    print_results,
    semantic_hash,
)

log = logging.getLogger(__name__)


def send_design(design: Design, conn: Connection, remote_path: str) -> Tuple[str, str]:
    root_path = design.root_path

    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_dir = Path(tmpdirname)
        zip_file = temp_dir / f"{design.name}.zip"
        log.debug(f"temp_dir={temp_dir} zip_file={zip_file}")
        new_design: Dict[str, Any] = {}  # **design.dict(), "design_root": None}
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
        design_file = temp_dir / f"{design.name}.json"
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

        conn.put(zip_file, remote=remote_path)
        return zip_file.name, design_file.name


def remote_runner(channel, remote_path, zip_file, flow, design_file, flow_settings={}, env=None):
    import os
    import zipfile

    from xeda.flow_runner import DefaultRunner  # pyright: ignore reportMissingImports

    os.chdir(remote_path)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    while not channel.isclosed():
        with zipfile.ZipFile(zip_file, mode="r") as archive:
            archive.extractall(path=remote_path)

        xeda_run_dir = "remote_run"
        launcher = DefaultRunner(
            xeda_run_dir,
            cached_dependencies=True,
            incremental=False,
            post_cleanup=False,
            # post_cleanup_purge = True,
        )
        f = launcher.run(
            flow,
            design=design_file,
            flow_settings=flow_settings,
        )
        results = f.results.to_dict()
        channel.send(results)


def get_env_var(conn, var):
    result = conn.run(f"echo ${var}", hide=True, pty=False)
    assert result.ok
    return result.stdout.strip()


class RemoteRunner(FlowLauncher):
    def run_remote(
        self,
        design: Union[str, Path, Design],
        flow_name: str,
        host: str,
        user: Optional[str] = None,
        port: Optional[int] = None,
        flow_settings=[],
    ):
        if not isinstance(design, Design):
            design = Design.from_file(design)
        design_hash = semantic_hash(
            dict(
                # design=design,
                rtl_hash=design.rtl_hash,  # TODO WHY?!!
                tb_hash=design.tb_hash,
            )
        )
        conn = Connection(host=host, user=user, port=port)
        remote_home = get_env_var(conn, "HOME")
        remote_path = f"{remote_home}/.xeda/{design.name}_{design_hash[:DIR_NAME_HASH_LEN]}"
        if not Transfer(conn).is_remote_dir(remote_path):
            conn.sftp().mkdir(remote_path)
        assert Transfer(conn).is_remote_dir(remote_path)
        conn.sftp().chdir(remote_path)
        zip_file, design_file = send_design(design, conn, remote_path)

        flowrun_hash = semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                xeda_version=__version__,
            ),
        )

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

        import execnet

        ssh_opt = f"{host}"
        if user:
            ssh_opt = f"{user}@{ssh_opt}"
        if port:
            ssh_opt = f"-p {port} {ssh_opt}"

        gw = execnet.makegateway(f"ssh={ssh_opt}//chdir={remote_path}")
        channel = gw.remote_exec(
            """
            import sys, os
            channel.send((sys.platform, tuple(sys.version_info), os.getpid()))
        """
        )
        platform, version_info, _ = channel.receive()
        log.info(
            f">>{host}: {platform} {'.'.join(str(v) for v in version_info)} path={get_env_var(conn, 'PATH')} shell={get_env_var(conn, 'SHELL')}"
        )
        assert version_info[0] == 3
        assert version_info[1] >= 8

        channel = gw.remote_exec(
            remote_runner,
            remote_path=remote_path,
            zip_file=zip_file,
            flow=flow_name,
            design_file=design_file,
            flow_settings=flow_settings,
        )

        results = channel.receive()
        gw.exit()
        if results:
            print_results(
                results=results,
                title=f"Results of flow:{flow_name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

            dump_json(results, results_json_path, backup=True)
            log.info("Results written to %s", results_json_path)
        return results
