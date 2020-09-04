from copy import deepcopy
import json
import math
import re
import shutil
import sys
import csv
from pathlib import Path
from typing import List
from ..flows import Settings, try_convert
from . import Plugin, PostResultsPlugin, ReplicatorPlugin


class LwcSim(PostResultsPlugin, ReplicatorPlugin):
    def replicate_settings_hook(self, settings: Settings) -> List[Settings]:
        #FIXME WOOOOOOPS so this doesn't actually work, will revert/fix ASAP

        vf_file = Path('variants_formulas.json')
        if vf_file.exists():
            with open(vf_file) as f:
                all_variants_formulas = json.load(f)
        else:
            all_variants_formulas = self.extract_formulas()
            if all_variants_formulas:
                with open(vf_file, 'w') as f:
                    json.dump(all_variants_formulas, f, indent=4)

        if not all_variants_formulas:
            self.logger.critical(
                f'Failed to read `variants_formulas.json` or parse `variants.txt`. Please make sure `variants_formulas.json` exists and is in correct format.')
            return
        
        self.all_variants_formulas = all_variants_formulas

        # #TODO FIXME!!!

        # replicated = []
        # for variant_id in ["V3"]: 
        #     s = deepcopy(settings)

        #     s.design['tb_generics'] = {
        #         "G_FNAME_PDI": {
        #             "file": f"KAT/{variant_id}/pdi.txt"
        #         },
        #         "G_FNAME_SDI": {
        #             "file": f"KAT/{variant_id}/sdi.txt"
        #         },
        #         "G_FNAME_DO": {
        #             "file": f"KAT/{variant_id}/do.txt"
        #         },
        #         "G_TEST_MODE": 4
        #     }

        #     s.variant_id = variant_id
        #     replicated.append(s)

        settings.variant_id = settings.design['variant_id']

        # return replicated
        return [settings]

    def extract_formulas(self):
        print("begin")
        variants_txt = Path.cwd() / 'docs' / 'variants.txt'
        if not variants_txt.exists():
            self.logger.critical(
                f'Variants file {variants_txt} does not exist! Try specifying formulas manually in `variants_formulas.json` file')
            return None

        # TODO both sections f and g must exist with these titles?
        formulas_section_re = re.compile(
            r'''f\.\s+Execution\s+times\s*(Execution\s+time\s+of.*)\s*g. Latencies''', re.MULTILINE | re.IGNORECASE | re.DOTALL)

        formulas_re = re.compile(
            r'''(\s*Execution\s+time\s+of\s+(?P<name>[^:]*):\s*^\s*(?P<formula>.+))\s*''', re.MULTILINE | re.IGNORECASE)

        ae_re = re.compile(r'authenticated\s*encryption', re.IGNORECASE)
        ad_re = re.compile(r'authenticated\s*decryption', re.IGNORECASE)
        hash_re = re.compile(r'hashing', re.IGNORECASE)

        sizes_re = re.compile(r'''AD block size=(?P<ad_block_size>\d+)\s+Msg/Cph block size=(?P<msg_block_size>\d+)\s+Hash block size=(?P<hash_block_size>\d+)''', re.IGNORECASE | re.MULTILINE)


        operation_formulas = {}

        self.logger.info(f'Extracting formulas from {variants_txt}')

        def parse_all_variants():
            variants = []
            variant_id = None
            variant_desc = None
            variant_body = []
            variant_begin_re = re.compile(r'^\s*(?P<id>v\d+):?\s+(?P<desc>.*)', re.IGNORECASE)

            def add_variant():
                if variant_id:
                    variants.append({'id': variant_id, 'desc': variant_desc, 'body': ''.join(variant_body)})
            with open(variants_txt) as f:
                for line in f.readlines():
                    match = variant_begin_re.match(line)
                    if match:
                        add_variant()
                        variant_id = match.group('id')
                        variant_desc = match.group('desc')
                        variant_body = []
                    elif variant_id:
                        variant_body.append(line)
            add_variant()
            self.logger.info(f'Found {len(variants)} variants!')
            return variants

        all_variants = parse_all_variants()
        if not all_variants:
            return None

        all_variants_formulas = {}


        for variant in all_variants:
            content = variant['body']

            variant_json = {}

            match = sizes_re.search(content)
            if match:
                for sz in ['ad_block_size', 'msg_block_size', 'hash_block_size']:
                    variant_json[sz] = match.group(sz)
                self.logger.warning(
                    f'Could parse sizes from variants.txt')

            match = formulas_section_re.search(content)
            if match:
                content = match.group(1)
            # else:
            #     self.logger.warning(
            #         f'Could not parse "Execution times" section in variant description. Trying whole content.')

            match = [m.groupdict() for m in formulas_re.finditer(content)]

            if not match:
                self.logger.critical(f'Could not parse execution times formulas in variant description')
                return None

            for operation in match:
                formula = operation['formula']
                name = operation['name']
                operation = 'AE' if ae_re.match(name) else 'AD' if ad_re.match(
                    name) else 'HASH' if hash_re.match(name) else name
                self.logger.debug(f'Formula for {name} ({operation}): {formula}')
                operation_formulas[operation] = formula
            
            print(variant_json)
            variant_json.update({'desc': variant['desc'], 'formulas': operation_formulas})
            print(variant_json)

            all_variants_formulas[variant['id']] = variant_json

        return all_variants_formulas

    # PostResults Hook
    def post_results_hook(self, run_dir, settings):
        """ Check timing vs formula for the variant """

        logger = self.logger

        if settings.active_flow != 'sim':
            logger.info(f"LwcSim hooks only work on 'sim' flows but active flow was {settings.active_flow}")
            return

        variant_id = settings.variant_id


        self.logger.info(f"using formulas for variant {variant_id}")
        vari = self.all_variants_formulas.get(variant_id)
        if not vari:
            self.logger.critical(f"Could not find variant data for variant ID: {variant_id}")
            return
        operation_formulas = vari['formulas']

        self.logger.info(operation_formulas)

        allowed_funcs = {
            k: v for k, v in math.__dict__.items() if not k.startswith("__")
        }

        # Na, Nm, Nc, Nh: the number of complete blocks of associated data, plaintext, ciphertext, and hash message, respectively
        # Ina, Inm, Inc, Inh: binary variables equal to 1 if the last block of the respective data type is incomplete, and 0 otherwise
        # Bla, Blm, Blc, and Blh: the number of bytes in the incomplete block of associated data, plaintext, ciphertext, and hash message, respectively
        variable_names = ['Na', 'Nm', 'Nc', 'Nh', 'Ina', 'Inm', 'Inc', 'Inh', 'Bla', 'Blm', 'Blc', 'Blh']

        # TODO use G_FNAME_TIMING_CSV
        timing_csv_path = run_dir / "timing.csv"
        shutil.copy(timing_csv_path, run_dir / f"timing_{variant_id}.csv")

        with open(timing_csv_path, newline="") as in_csv, open(run_dir / f"timing_vs_formula_{variant_id}.csv", "w") as out_csv:
            reader = csv.DictReader(in_csv)
            t_formula_header = "Theoretical Execution Time"
            diff_header = "Absolute Difference"
            writer = csv.DictWriter(out_csv, fieldnames=reader.fieldnames +
                                    [t_formula_header, diff_header])
            writer.writeheader()
            max_diff = 0
            max_diff_percent = 0
            for row in reader:
                operation = row['Operation']
                msg_size = int(row['Msg Size'])
                ad_size = int(row['AD Size'])
                new_key = bool(int(row['New Key']))
                if operation not in operation_formulas:
                    sys.exit(f'No formula found for operation: {operation}')
                # row['Na'] = ad_size/
                variables = dict((k, try_convert(row.get(k))) for k in variable_names)
                t_formula = eval(operation_formulas[operation], allowed_funcs, variables)
                t_sim = int(row['Execution Time'])
                diff = abs(t_formula - t_sim)
                if diff > max_diff:
                    max_diff = diff
                    max_diff_percent = diff * 100 / t_sim
                writer.writerow({**row, t_formula_header: t_formula, diff_header: diff})

            if max_diff > 0:
                self.logger.warning(
                    f'Maximum discrepancy between formula and simulation time was {max_diff} cycles ({max_diff_percent:.1f}%).')

