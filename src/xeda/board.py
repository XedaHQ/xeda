import logging
import os
from typing import Any, Dict, Optional, Union

import pkg_resources

from .dataclass import root_validator
from .flow import FPGA, FpgaSynthFlow
from .utils import toml_load, toml_loads

__all__ = [
    "get_board_file_path",
    "get_board_data",
    "WithFpgaBoardSettings",
]

log = logging.getLogger(__name__)


def get_board_file_path(file: str):
    return pkg_resources.resource_filename("xeda.data.boards", file)


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
        log.info("Retrieving board data for %s", board)
        board_toml_bytes = pkg_resources.resource_string("xeda.data", "boards.toml")
        if board_toml_bytes:
            boards_data = toml_loads(board_toml_bytes.decode("utf-8"))
        else:
            log.error(
                "Unable to get resource %s.%s. Please check xeda installation.",
                "xeda.data",
                "boards.toml",
            )
    return boards_data.get(board)


class WithFpgaBoardSettings(FpgaSynthFlow.Settings):
    board: Optional[str] = None
    custom_boards_file: Optional[str] = None

    @root_validator(pre=True)
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
