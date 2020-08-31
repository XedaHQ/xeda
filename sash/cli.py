from .flows.diamond import Diamond
from .flows.vivado import Vivado

from .sash_app import SashApp

def run_sash(args=None):
    sash = SashApp()
    sash.register_suites(Diamond, Vivado)
    
    sash.main()

