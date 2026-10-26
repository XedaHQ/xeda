"""Settings as the command line gives them."""

from collections.abc import Iterable

from xeda.dataclass import SettingText


def text_items(texts: Iterable[str]) -> dict[str, SettingText]:
    """Return `KEY=VALUE` texts as a table of command-line text by dotted key.

    A caller that hands a table to the launcher gets the same result as one that hands the list
    of texts: each value is read as the type of its setting."""
    items = (text.split("=", 1) for text in texts)
    return {key: SettingText(value) for key, value in items}
