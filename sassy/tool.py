import json
import os
import sys
import logging
import re
import tempfile
from pathlib import Path
import shutil
import subprocess
import importlib

from string import Template




try:
    import importlib.resources as importlib_resources
except ImportError:
#     # Try backported to PY<37 `importlib_resources`.
    import importlib_resources




class Tool:
    """ All tools inherit from Tool"""
    name = None
    executable = None
    settings_file = Path.cwd() / 'config.json'
    

    def __init__(self):
        self.data = dict()
        self.settings = dict()
        self.load_settings()
        
    def report_path(self):
        pass
    
    def dump_data(self):
        with open('synth_results.json', 'w') as outfile:
            json.dump(self.data, outfile, indent=4)
            
    def load_settings(self):
        with open(self.settings_file) as f:
            self.settings = json.load(f)    
            
    def dump_settings(self):
        with open(self.settings_file, 'w') as outfile:
            json.dump(self.settings, outfile, indent=4)
            
    def copy_from_template(self, subpkg_path, script_name, replace):
        resource = importlib_resources.read_text(subpkg_path, script_name)
        template = Template(resource)
        script_content = template.safe_substitute()
        script_path = Path.cwd() / script_name
        with open(script_path, 'w') as f:
            f.write(script_content)
        return script_path
    
    def run_process(self, args):
        proc = subprocess.run(args)
        proc.check_returncode()
        return proc


class SymbiFlow(Tool):
    
    def analyze(self):
        self.run_process(f'ghdl -i {GHDL_COMMON_OPTS} ../src_rtl/*.vhd ../src_rtl/LWC/*.vhd ../src_tb/LWC_TB.vhd')
        self.run_process(f'ghdl -m {GHDL_COMMON_OPTS} LWC_TB')
        
        # nextpnr-ecp5 --25k --freq 74 --package CABGA381 --speed 6 --json yosys.json


