import logging
import os
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Dict, Optional, Union

from importlib_resources import as_file, files

from .dataclass import Field, model_validator
from .flow import FPGA, FpgaSynthFlow
from .utils import expand_env_vars, toml_load

__all__ = [
    "WithFpgaBoardSettings",
    "get_board_data",
]

log = logging.getLogger(__name__)


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
    custom_boards_file: Path | None = Field(
        None,
        description="Path to a TOML board database, used instead of the bundled database. "
        "Relative paths are resolved against the design directory.",
    )

    def board_data(self) -> dict[str, Any] | None:
        """Read this flow's selected board from its configured database."""
        return get_board_data(self.board, self.custom_boards_file)

    def board_file(self, name: str) -> AbstractContextManager[Path]:
        """A file named relative to the board database, such as a board's local `lpf`.

        A context manager: a bundled file may exist on disk only while it is open.
        """
        if self.custom_boards_file:
            return nullcontext(self.custom_boards_file.parent / name)
        return as_file(files("xeda.data").joinpath(name))

    @staticmethod
    def _resolve_boards_path(value: Any, context: dict[str, Any]) -> Any:
        if not isinstance(value, (str, os.PathLike)) or not value:
            return value
        root = context.get("design_root")
        path = expand_env_vars(
            Path(value),
            {
                "DESIGN_ROOT": root,
                "DESIGN_DIR": root,
                "PWD": context.get("runner_cwd"),
            },
        )
        return Path(root) / path if root is not None and not path.is_absolute() else path

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "custom_boards_file":
            value = self._resolve_boards_path(value, self.context)
        super().__setattr__(name, value)

    @model_validator(mode="before")
    @classmethod
    def _fpga_validate(cls, values: Dict[str, Any], info) -> Dict[str, Any]:
        if info.field_name is not None and info.data is None:
            if info.field_name not in ("board", "custom_boards_file"):
                return values  # an assignment that changes neither the board nor its database
        board_name = values.get("board")
        log.debug("_fpga_validate! board_name=%s", board_name)
        fpga = values.get("fpga")
        context = info.context or {}
        custom = cls._resolve_boards_path(values.get("custom_boards_file"), context)
        if custom is not None:
            values["custom_boards_file"] = custom
        if board_name:
            database = f"custom boards file {custom}" if custom else "the bundled board database"
            try:
                board_data = get_board_data(board_name, custom)
            except OSError as e:
                raise ValueError(f"Cannot read {database}: {e}") from e
            if board_data is not None and not isinstance(board_data, dict):
                raise ValueError(f"Board {board_name!r} must be a table in {database}")
            if fpga:
                return values
            if board_data:
                board_fpga = board_data.get("fpga")
                log.info("FPGA info for board %s: %s", board_name, str(board_fpga))
                if board_fpga:
                    if isinstance(board_fpga, str):
                        board_fpga = {"part": board_fpga}
                    values["fpga"] = FPGA(**board_fpga)
        return values
