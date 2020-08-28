# © 2020 [Kamyar Mohajerani](malto:kamyar@ieee.org)

class Settings:
    def __init__(self) -> None:
        self.flow = dict()
        self.design = dict()
        self.run = dict()


def try_convert(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            s = str(s)
            if s.lower in ['true', 'yes']:
                return True
            if s.lower in ['false', 'no']:
                return False
            return s

