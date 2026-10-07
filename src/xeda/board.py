import logging
import os
import tomllib
from contextlib import AbstractContextManager, nullcontext
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from importlib_resources import as_file, files

from .dataclass import Field, model_validator
from .flow import FPGA, FpgaSynthFlow
from .utils import expand_env_vars, toml_load, toml_loads
from .yaml_loader import load_yaml, yaml_error_position

__all__ = [
    "BOARD_DATABASE_FORMATS",
    "WithFpgaBoardSettings",
    "board_database_format",
    "bundled_boards",
    "canonical_board_name",
    "get_board_data",
    "read_board_database",
]

log = logging.getLogger(__name__)

#: The formats a custom board database (`custom_boards_file`) may be written in, by file suffix.
#: The databases xeda bundles stay TOML.
BOARD_DATABASE_FORMATS: Dict[str, str] = {".toml": "toml", ".yaml": "yaml", ".yml": "yaml"}


def board_database_format(path: Union[str, os.PathLike]) -> str:
    """The format (`toml` or `yaml`) custom board database `path` is read in, by its suffix.

    Suffixes are case-sensitive, like every name xeda reads. Any other suffix is a `ValueError`
    naming the file and the suffixes accepted, never a guess.
    """
    suffix = Path(path).suffix
    fmt = BOARD_DATABASE_FORMATS.get(suffix)
    if fmt is not None:
        return fmt
    accepted = ", ".join(repr(known) for known in BOARD_DATABASE_FORMATS)
    what = f"unsupported file suffix {suffix!r}" if suffix else "no file suffix"
    if suffix.lower() in BOARD_DATABASE_FORMATS:
        what += f" (suffixes are case-sensitive: did you mean {suffix.lower()!r}?)"
    reason = f"{what}; a board database is TOML or YAML ({accepted})"
    raise ValueError(f'Cannot load board database "{path}": {reason}')


def read_board_database(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    """The table of boards in custom board database `path`, TOML or YAML by its suffix.

    YAML goes through xeda's one strict reader (`xeda.yaml_loader`): YAML 1.2 core scalars,
    string keys only, duplicate keys an error. Every way the content can be wrong is a
    `ValueError` naming the file (and the line, where the parser knows it); a file that cannot be
    opened is the `OSError`.
    """
    fmt = board_database_format(path)
    try:
        if fmt == "toml":
            data = toml_load(path)
        else:
            data = load_yaml(Path(path))
    except UnicodeDecodeError as e:
        raise ValueError(
            f'Cannot load board database "{path}": not UTF-8 text: {e.reason} at byte {e.start}'
        ) from None
    except yaml.reader.ReaderError as e:
        raise ValueError(
            f'Cannot load board database "{path}": not UTF-8 text: {e.reason} at byte {e.position}'
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f'Cannot load board database "{path}": {e}') from None
    except yaml.MarkedYAMLError as e:
        reason, line, column = yaml_error_position(e)
        where = f", line {line}, column {column}" if line is not None else ""
        raise ValueError(f'Cannot load board database "{path}"{where}: {reason}') from None
    except yaml.YAMLError as e:
        raise ValueError(f'Cannot load board database "{path}": {e}') from None
    if data is None:  # an empty YAML document, as an empty TOML file is an empty table
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f'Cannot load board database "{path}": a board database is a table (mapping) of '
            f"boards by name, not a {type(data).__name__}"
        )
    return data


def bundled_boards() -> Dict[str, Any]:
    """The boards xeda bundles (`xeda/data/boards.toml`), by name.

    A bundled board is stored under a lower-case name and found by its name in any letter case
    (`get_board_data`). A name that is not lower case would make that lookup ambiguous, so such a
    database is refused: two names that differ only in case cannot both be in it.
    """
    boards = toml_loads(files("xeda.data").joinpath("boards.toml").read_text())
    odd = [name for name in boards if name != name.lower()]
    if odd:
        raise ValueError(
            "The bundled board database xeda/data/boards.toml must name its boards in lower case: "
            + ", ".join(map(repr, odd))
        )
    return boards


def canonical_board_name(
    board: Any, custom_boards_file: Union[None, str, os.PathLike] = None
) -> Any:
    """The name a selected board is stored under, so that every spelling is one setting.

    A bundled board answers to its name in any letter case and is stored in lower case. A board of
    a custom database is stored as written: its names are case-sensitive. A name no board answers
    to is returned as written, so the error that names it shows what was given.
    """
    if custom_boards_file or not isinstance(board, str):
        return board
    return board.lower() if board.lower() in bundled_boards() else board


def get_board_data(
    board: Optional[str], custom_boards_file: Union[None, str, os.PathLike] = None
) -> Optional[Dict[str, Any]]:
    """The entry for `board`: from `custom_boards_file` (TOML or YAML), else from the bundled
    (TOML) database.

    A bundled board is found by its name in any letter case. The names in a custom database are
    case-sensitive: `board` must be written exactly as the database writes it.
    """
    if not board:
        return None
    if custom_boards_file:
        log.debug("Retrieving board data for %s from %s", board, custom_boards_file)
        boards_data = read_board_database(custom_boards_file)
        name = board
        database = str(custom_boards_file)
    else:
        boards_data = bundled_boards()
        name = board.lower() if isinstance(board, str) else board
        database = "the bundled board database xeda/data/boards.toml"
        if name in boards_data:
            log.debug("Retrieved board data for %s", name)
    if name not in boards_data:
        suggestions = get_close_matches(name, boards_data) if isinstance(name, str) else []
        hint = f". Did you mean {', '.join(map(repr, suggestions))}?" if suggestions else ""
        raise ValueError(f"Unknown board {board!r} in {database}{hint}")
    return boards_data[name]


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
        "files) from the board database. See `xeda list-boards`. A bundled board is found by its "
        "name in any letter case; a name in a custom database is case-sensitive.",
    )
    custom_boards_file: Path | None = Field(
        None,
        description="Path to a board database, in TOML or YAML (by the file's suffix: `.toml`, "
        "`.yaml` or `.yml`), used instead of the bundled database. Relative paths are resolved "
        "against the design directory; a board's local `lpf` resolves relative to this file. "
        "The board names in it are case-sensitive.",
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
    def _board_fpga(board_data: dict[str, Any] | None) -> FPGA | None:
        """Build the device named by a board entry, if it provides one."""
        if not board_data or not (value := board_data.get("fpga")):
            return None
        return FPGA(**({"part": value} if isinstance(value, str) else value))

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
        board_derived_fpga = False
        if name in ("board", "custom_boards_file") and self.board and self.fpga:
            board_derived_fpga = self.fpga == self._board_fpga(self.board_data())
        if name == "custom_boards_file":
            value = self._resolve_boards_path(value, self.context)
        elif name == "board":
            value = canonical_board_name(value, self.custom_boards_file)
        super().__setattr__(name, value)
        if board_derived_fpga:
            # A board or database change invalidates the device obtained from the old board.
            # A different, explicitly selected FPGA remains the caller's choice.
            self.fpga = self._board_fpga(self.board_data())

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
            if custom and isinstance(custom, (str, os.PathLike)):
                board_database_format(custom)  # a suffix xeda does not read, even with no board
        if board_name:
            database = f"custom boards file {custom}" if custom else "the bundled board database"
            try:
                board_data = get_board_data(board_name, custom)
            except OSError as e:
                raise ValueError(f"Cannot read {database}: {e}") from e
            # `get_board_data` gives None only for a selected board whose entry has no value
            # (`MY_BOARD:` in YAML; TOML cannot write one): no board at all never reaches here.
            if board_data is None:
                raise ValueError(
                    f"Board {board_name!r} has no value in {database}: give it a table of its "
                    "settings (`{}` for a board with none)"
                )
            if not isinstance(board_data, dict):
                raise ValueError(f"Board {board_name!r} must be a table in {database}")
            if "name" in board_data:
                raise ValueError(
                    f"Board {board_name!r} in {database}: `name` was removed: use "
                    "`openfpgaloader_board`, the board's name in openFPGALoader"
                )
            loader = board_data.get("openfpgaloader_board")
            if loader is not None and not (isinstance(loader, str) and loader.strip()):
                raise ValueError(
                    f"Board {board_name!r} in {database}: `openfpgaloader_board` is the board's "
                    f"name in openFPGALoader, as text, not {loader!r}; leave it out for a board "
                    "openFPGALoader does not know"
                )
            values["board"] = canonical_board_name(board_name, custom)
            if fpga:
                return values
            if board_data:
                board_fpga = cls._board_fpga(board_data)
                log.debug("FPGA info for board %s: %s", values["board"], board_fpga)
                if board_fpga:
                    values["fpga"] = board_fpga
        return values
