import logging
import os
from typing import Any, Dict, Optional, Union

from importlib_resources import as_file, files

from .dataclass import Field, model_validator
from .flow import FPGA, FpgaSynthFlow
from .utils import toml_load

__all__ = [
    "WithFpgaBoardSettings",
    "get_board_data",
    "get_board_file_path",
]

log = logging.getLogger(__name__)


def get_board_file_path(file: str):
    # FIXME refactor and verify behavior
    # return pkg_resources.resource_filename("xeda.data.boards", file)
    res = files("xeda.data.boards").joinpath(file)
    return as_file(res)


def get_board_data(
    board: Optional[str], custom_toml_file: Union[None, str, os.PathLike] = None
) -> Optional[Dict[str, Any]]:
    if not board:
        return None
    boards_data = {}
    if custom_toml_file:
        log.debug("Retrieving board data for %s from %s", board, custom_toml_file)
        boards_data = toml_load(custom_toml_file)
    else:
        res = files("xeda.data").joinpath("boards.toml")
        with as_file(res) as p:
            boards_data = toml_load(p)
        if boards_data and board in boards_data:
            log.info("Retrieved board data for %s", board)
        # else:
        #     log.error(
        #         "Unable to get resource %s.%s. Please check xeda installation.",
        #         "xeda.data",
        #         "boards.toml",
        #     )
    return boards_data.get(board)


#: How to give a flow whose settings take a `board` its device, for `Flow.required_settings`.
FPGA_OR_BOARD_REQUIRED = (
    "the target FPGA device: give its part number with `-s fpga.part=<part>`, or a board that has "
    "one with `-s board=<name>` (see `xeda list-boards`); in the design file, as `fpga.part` or "
    "`board` in its `[flows.{flow}]` section"
)


class WithFpgaBoardSettings(FpgaSynthFlow.Settings):
    board: Optional[str] = Field(
        None,
        description="Target development board. Fills in `fpga` (and board-specific constraint "
        "files) from the board database. See `xeda list-boards`.",
    )
    custom_boards_file: Optional[str] = Field(
        None,
        description="Path to a TOML file with additional board definitions, used instead of the "
        "bundled board database.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fpga_validate(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        board_name = values.get("board")
        log.debug("_fpga_validate! board_name=%s", board_name)
        fpga = values.get("fpga")
        if not fpga and board_name:
            board_data = get_board_data(board_name)
            if board_data:
                board_fpga = board_data.get("fpga")
                log.info("FPGA info for board %s: %s", board_name, str(board_fpga))
                if board_fpga:
                    if isinstance(board_fpga, str):
                        board_fpga = {"part": board_fpga}
                    values["fpga"] = FPGA(**board_fpga)
        return values
