"""Type definitions"""
import os
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path, os.PathLike[Any]]
