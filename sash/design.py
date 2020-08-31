
class SourceFile:
    def __init__(self, path, sim_only, language, standard):
        self.path = path
        self.sim_only = sim_only
        self.language = language
        self.standard = standard


class Design:
    """ captures and RTL design  """

    def __init__(self, name, source_files, top="LWC", tb_top="LWC_TB"):
        self.name = name
        self.source_files = source_files
        self.top = top
        self.tb_top = tb_top

    def sources(self, filter_sim_only=False, filter_language=None):
        return [src for src in self.source_files
                if (filter_language is None or (src.language == filter_language)) and
                (not filter_sim_only or src.sim_only)]
