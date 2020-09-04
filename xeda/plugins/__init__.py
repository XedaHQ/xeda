

from typing import Set
from xeda.flows import Settings
from typing import List

from pathlib import Path

class Plugin():
    def __init__(self, logger) -> None:
        self.logger = logger


class ReplicatorPlugin(Plugin):
    def replicate_settings_hook(self, settings: Settings) -> List[Settings]:
        return [settings]


class PostRunPlugin(Plugin):
    def post_run_hook(self, run_dir: Path, settings: Settings) -> None:
        if self:
            raise NotImplementedError


class PostResultsPlugin(Plugin):
    def post_results_hook(self, run_dir: Path, settings: Settings) -> None:
        if self:
            raise NotImplementedError
