import json
import math
import re
import sys
import csv
from pathlib import Path
from ..flows import try_convert
from . import Plugin


class LwcSimTiming(Plugin):
    name = 'LwcSimTiming'

    def extract_formulas(self):
        variants_txt = self.run_dir.parents[1] / 'docs' / 'variants.txt'
        if not variants_txt.exists():
            self.logger.critical(
                f'Variants file {variants_txt} does not exist! Try specifying formulas manually in `formulas.json` file')
            return None

        # TODO both sections f and g must exist with these titles?
        formulas_section_re = re.compile(
            r'''f\.\s+Execution\s+times\s*(Execution\s+time\s+of.*)\s*g. Latencies''', re.MULTILINE | re.IGNORECASE | re.DOTALL)

        formulas_re = re.compile(
            r'''(\s*Execution\s+time\s+of\s+(?P<name>[^:]*):\s*^\s*(?P<formula>.+))\s*''', re.MULTILINE | re.IGNORECASE)

        ae_re = re.compile(r'authenticated\s*encryption', re.IGNORECASE)
        ad_re = re.compile(r'authenticated\s*decryption', re.IGNORECASE)
        hash_re = re.compile(r'hashing', re.IGNORECASE)

        func_formulas = {}

        self.logger.info(f'Extracting formulas from {variants_txt}')

        def parse_all_variants():
            variants = []
            variant_id = None
            variant_desc = None
            variant_body = []
            variant_begin_re = re.compile(r'^\s*v(?P<id>\d+):?\s+(?P<desc>.*)', re.IGNORECASE)

            def add_variant():
                if variant_id:
                    variants.append({'id': variant_id, 'desc': variant_desc, 'body': ''.join(variant_body)})
            with open(variants_txt) as f:
                for line in f.readlines():
                    match = variant_begin_re.match(line)
                    if match:
                        add_variant()
                        variant_id = int(match.group('id'))
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

            for method in match:
                formula = method['formula']
                name = method['name']
                func_id = 'ae' if ae_re.match(name) else 'ad' if ad_re.match(
                    name) else 'hash' if hash_re.match(name) else name
                self.logger.debug(f'Formula for {name} ({func_id}): {formula}')
                func_formulas[func_id] = formula

            all_variants_formulas[variant['id']] = {'desc': variant['desc'], 'formulas': func_formulas}

        return all_variants_formulas

    def post_results_hook(self):
        # TODO FIXME
        variant_id = "1"

        variant_id = str(variant_id)

        vf_file = Path('variants_formulas.json')
        if vf_file.exists():
            with open(vf_file) as f:
                all_variants_formulas = json.load(f)
        else:
            all_variants_formulas = self.extract_formulas()
            with open(vf_file, 'w') as f:
                json.dump(all_variants_formulas, f, indent=4)

        if not all_variants_formulas:
            self.logger.critical(f'Failed to parse forumlas from variants.txt. Exiting')
            return

        self.logger.info(f"using formulas for variant {variant_id}")
        method_formulas = all_variants_formulas.get(variant_id)['formulas']

        self.logger.info(method_formulas)

        allowed_funcs = {
            k: v for k, v in math.__dict__.items() if not k.startswith("__")
        }

        int2id_map = {0: 'ae', 1: 'ad', 2: 'hash'}

        # Na, Nm, Nc, Nh: the number of complete blocks of associated data, plaintext, ciphertext, and hash message, respectively
        # Ina, Inm, Inc, Inh: binary variables equal to 1 if the last block of the respective data type is incomplete, and 0 otherwise
        # Bla, Blm, Blc, and Blh: the number of bytes in the incomplete block of associated data, plaintext, ciphertext, and hash message, respectively
        variable_names = ['Na', 'Nm', 'Nc', 'Nh', 'Ina', 'Inm', 'Inc', 'Inh', 'Bla', 'Blm', 'Blc', 'Blh']

        with open(self.run_dir / "timing.csv", newline="") as in_csv, open(self.run_dir / "timing_vs_formula.csv", "w") as out_csv:
            reader = csv.DictReader(in_csv)
            t_formula_header = "Theorhetical Execution Time"
            diff_header = "Absolute Difference"
            writer = csv.DictWriter(out_csv, fieldnames=reader.fieldnames +
                                    [t_formula_header, diff_header])
            writer.writeheader()
            max_diff = 0
            max_diff_percent = 0
            for row in reader:
                variables = dict((k, try_convert(row.get(k))) for k in variable_names)
                method_id = int(row['Hash']) * 2 + int(row['AE/AD'])
                method_id = int2id_map[method_id]
                if method_id not in method_formulas:
                    sys.exit(f'no forumula for method:{method_id}')
                t_formula = eval(method_formulas[method_id], allowed_funcs, variables)
                t_sim = int(row['Execution Time'])
                diff = abs(t_formula - t_sim)
                if diff > max_diff:
                    max_diff = diff
                    max_diff_percent = diff * 100 / t_sim
                writer.writerow({**row, t_formula_header: t_formula, diff_header: diff})

            if max_diff > 0:
                self.logger.warning(
                    f'Maximum discrepancy between formula and simulation time was {max_diff} cycles ({max_diff_percent:.1f}%).')

