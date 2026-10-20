"""The local record of a detached launch: what `xeda remote attach` needs to fetch its result.

A remote run that is detached writes this record in the mirror, `<mirror>/.launches/<id>.json`,
before the launch starts. The record holds the local side of the request: the paths of the local
files that were sent, the run directories of the local plan, and the names that the result is
shown with. It holds nothing secret and nothing that the remote host does not know. Paths of this
machine stay on this machine, so the remote never stores them.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..dataclass import XedaBaseModel
from ..utils import replacing_file
from .errors import RemoteError

RECORDS = ".launches"


class LaunchRecord(XedaBaseModel):
    """The local side of one detached launch."""

    id: str
    host: str
    chain: str
    flow: str
    #: The node that the request names, and the name of the design.
    requested: str
    design_name: str
    #: The design as the command line gave it, if it was a file or a name.
    design: Optional[str]
    target: Optional[str]
    #: The elements of the request, as `xeda run --json` shows them.
    request: List[Dict[str, Any]]
    #: The local run root, and the run directories of the local plan.
    local_root: str
    run_paths: List[str]
    remote_run_root: str
    inputs_dir: str
    #: The local file for each file that was sent, by its path in the inputs directory; the names
    #: of the files that xeda made for the remote; the root of the design here.
    files: Dict[str, str]
    generated: List[str]
    design_root: str
    fetch_all: bool
    #: Whether the request had `--outputs-to`. An attach does not deliver.
    delivers: bool
    debug: bool


def record_path(mirror_root: Path, launch_id: str) -> Path:
    return mirror_root / RECORDS / f"{launch_id}.json"


def write_record(mirror_root: Path, record: LaunchRecord) -> None:
    path = record_path(mirror_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with replacing_file(path, encoding="utf-8") as stream:
        stream.write(record.model_dump_json(indent=2))
        stream.write("\n")


def read_record(mirror_root: Path, launch_id: str) -> LaunchRecord:
    """The record of the launch, or a `RemoteError` that says how to go on without it."""
    path = record_path(mirror_root, launch_id)
    try:
        return LaunchRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RemoteError(
            f"This machine has no record of the launch {launch_id} ({path}), so it cannot place "
            "the results in the mirror. Run the original command again: it reuses a finished "
            "run."
        ) from None
    except (OSError, ValueError) as error:
        raise RemoteError(
            f"The record of the launch {launch_id} ({path}) is unusable: {error}"
        ) from None
