import logging
import math
import sys
import csv
import itertools
import json
from ..utils import try_convert
from ..flows.flow import Flow, SimFlow


# class LwcSim(PostResultsPlugin):
    # def extract_formulas(self):
    #     print("begin")
    #     variants_txt = Path.cwd() / 'docs' / 'variants.txt'
    #     if not variants_txt.exists():
    #         self.logger.critical(
    #             f'Variants file {variants_txt} does not exist! Try specifying formulas manually in `variants_formulas.json` file')
    #         return None

    #     # TODO both sections f and g must exist with these titles?
    #     formulas_section_re = re.compile(
    #         r'''f\.\s+Execution\s+times\s*(Execution\s+time\s+of.*)\s*g. Latencies''', re.MULTILINE | re.IGNORECASE | re.DOTALL)

    #     formulas_re = re.compile(
    #         r'''(\s*Execution\s+time\s+of\s+(?P<name>[^:]*):\s*^\s*(?P<formula>.+))\s*''', re.MULTILINE | re.IGNORECASE)

    #     ae_re = re.compile(r'authenticated\s*encryption', re.IGNORECASE)
    #     ad_re = re.compile(r'authenticated\s*decryption', re.IGNORECASE)
    #     hash_re = re.compile(r'hashing', re.IGNORECASE)

    #     sizes_re = re.compile(r'''AD block size=(?P<ad_block_size>\d+)\s+Msg/Cph block size=(?P<msg_block_size>\d+)\s+Hash block size=(?P<hash_block_size>\d+)''', re.IGNORECASE | re.MULTILINE)


    #     operation_formulas = {}

    #     self.logger.info(f'Extracting formulas from {variants_txt}')

    #     def parse_all_variants():
    #         variants = []
    #         variant_id = None
    #         variant_desc = None
    #         variant_body = []
    #         variant_begin_re = re.compile(r'^\s*(?P<id>v\d+):?\s+(?P<desc>.*)', re.IGNORECASE)

    #         def add_variant():
    #             if variant_id:
    #                 variants.append({'id': variant_id, 'desc': variant_desc, 'body': ''.join(variant_body)})
    #         with open(variants_txt) as f:
    #             for line in f.readlines():
    #                 match = variant_begin_re.match(line)
    #                 if match:
    #                     add_variant()
    #                     variant_id = match.group('id')
    #                     variant_desc = match.group('desc')
    #                     variant_body = []
    #                 elif variant_id:
    #                     variant_body.append(line)
    #         add_variant()
    #         self.logger.info(f'Found {len(variants)} variants!')
    #         return variants

    #     all_variants = parse_all_variants()
    #     if not all_variants:
    #         return None

    #     all_variants_formulas = {}


    #     for variant in all_variants:
    #         content = variant['body']

    #         variant_json = {}

    #         match = sizes_re.search(content)
    #         if match:
    #             for sz in ['ad_block_size', 'msg_block_size', 'hash_block_size']:
    #                 variant_json[sz] = match.group(sz)
    #             self.logger.warning(
    #                 f'Could parse sizes from variants.txt')

    #         match = formulas_section_re.search(content)
    #         if match:
    #             content = match.group(1)
    #         # else:
    #         #     self.logger.warning(
    #         #         f'Could not parse "Execution times" section in variant description. Trying whole content.')

    #         match = [m.groupdict() for m in formulas_re.finditer(content)]

    #         if not match:
    #             self.logger.critical(f'Could not parse execution times formulas in variant description')
    #             return None

    #         for operation in match:
    #             formula = operation['formula']
    #             name = operation['name']
    #             operation = 'AE' if ae_re.match(name) else 'AD' if ad_re.match(
    #                 name) else 'HASH' if hash_re.match(name) else name
    #             self.logger.debug(f'Formula for {name} ({operation}): {formula}')
    #             operation_formulas[operation] = formula
            
    #         print(variant_json)
    #         variant_json.update({'desc': variant['desc'], 'formulas': operation_formulas})
    #         print(variant_json)

    #         all_variants_formulas[variant['id']] = variant_json

    #     return all_variants_formulas

    # PostResults Hook
    # def post_results_hook(self, run_dir, settings, results):
    #     pass



# must be replicated and unique for every Flow instance
class LwcCheckTimingHook():
    """ Check timing vs formula for the variant """

    def __init__(self, variant_id, variant_data, gen_aead_timing, gen_hash_timing) -> None:
        self.variant_id = variant_id
        self.variant_data = variant_data
        self.gen_aead_timing = gen_aead_timing
        self.gen_hash_timing = gen_hash_timing

    def __call__(self, flow: Flow):
        logger = logging.getLogger()
        results = flow.results
        run_dir = flow.run_dir

        if not isinstance(flow, SimFlow):
            logger.info(f"LwcCheckTimingHook only operates on simulation flows.")
            return

        if not results["success"]:
            logger.critical("Not running post_results_hook because results are marked as failure.")
            return

        variant_id = self.variant_id
        variant = self.variant_data

        operations = variant.get('operations')
        if not operations:
            logger.critical(f"variants.json: missing operations for variant {variant_id}")
            return

        allowed_funcs = {
            k: v for k, v in math.__dict__.items() if not k.startswith("__")
        }

        # Na, Nm, Nc, Nh: the number of complete blocks of associated data, plaintext, ciphertext, and hash message, respectively
        # Ina, Inm, Inc, Inh: binary variables equal to 1 if the last block of the respective data type is incomplete, and 0 otherwise
        # Bla, Blm, Blc, and Blh: the number of bytes in the incomplete block of associated data, plaintext, ciphertext, and hash message, respectively
        variable_names = ['Na', 'Nm', 'Nc', 'Nh', 'Ina', 'Inm', 'Inc', 'Inh', 'Bla', 'Blm', 'Blc', 'Blh']

        timing_csv_path = run_dir / flow.settings.design['tb_generics']['G_FNAME_TIMING_CSV']

        out_csv_path = run_dir / f"timing_vs_formula_{variant_id}.csv"

        vivado_results = {}
        with open(run_dir / "vivado_sim_results.json", 'r') as resultsjson:
            vivado_results = json.load(resultsjson)

        vivado_results["variant"] = self.variant_id
        vivado_results["timing_csv"] = str(run_dir / f"timing_vs_formula_{variant_id}.csv")
        vivado_results["failed_vectors"] = str(run_dir / f"failed_test_vectors_{variant_id}.txt")
        logger.info(f"Saving timing comparison to {out_csv_path}")
        if self.gen_aead_timing:
            exectime_str = ""
            latency_str = ""
            vivado_results["short_timing_aead"] = str(run_dir / "AEAD_Timing.csv")
            with open(timing_csv_path, newline="") as in_csv, open(run_dir / f"AEAD_Timing.csv", 'w') as out_csv:
                reader = csv.DictReader(in_csv)
                next(reader)
                exectime = [] 
                latency = []
                for row in reader:
                    exectime.append(row["Actual Execution Time"]) 
                    latency.append(row["Actual Latency"])
                out_csv.write("AE\n")
                out_csv.write(','.join(exectime[0:5][::-1]) + '\n' + ','.join(exectime[5:10][::-1])  + '\n' + ','.join(exectime[10:15][::-1]) + '\n' + ','.join(latency[5:8][::-1]))
                out_csv.write("\nAD\n")
                out_csv.write(','.join(exectime[15:20][::-1]) + '\n' + ','.join(exectime[20:25][::-1])  + '\n' + ','.join(exectime[25:30][::-1]) + '\n' + ','.join(latency[20:23][::-1]))
        elif self.gen_hash_timing:
            exectime_str = ""
            vivado_results["short_timing_hash"] = str(run_dir / "HASH_Timing.csv")
            with open(timing_csv_path, newline="") as in_csv, open(run_dir / f"HASH_Timing.csv", 'w') as out_csv:
                reader = csv.DictReader(in_csv)
                next(reader)
                exectime = [] 
                for row in reader:
                    exectime.append(row["Actual Execution Time"]) 
                out_csv.write("HASH\n")
                out_csv.write(','.join(exectime[0:5][::-1]))
        
        outjsonpath = str(run_dir / "vivado_sim_results.json")
        with open(outjsonpath, 'w') as jsonfile:
            json.dump(vivado_results, jsonfile,indent=4)

        with open(timing_csv_path, newline="") as in_csv, open(out_csv_path, "w") as out_csv:
            reader = csv.DictReader(in_csv)
            t_exec_header = "Expected Execution Time"
            t_latency_header = "Expected Latency"
            exec_diff_header = "Actual-Expected Execution Time"
            latency_diff_header = "Actual-Expected Latency"
            writer = csv.DictWriter(out_csv, fieldnames=reader.fieldnames +
                                    [t_exec_header, t_latency_header, exec_diff_header, latency_diff_header])
            writer.writeheader()
            max_diff = 0
            max_diff_percent = 0
            for row in reader:
                op_id = row['Operation']
                msg_size = int(row['Msg Size'])
                ad_size = int(row['AD Size'])
                new_key = bool(int(row['New Key']))
                if op_id not in operations:
                    sys.exit(f'Operation {op_id} not specified for {variant_id}')
                operation = operations[op_id]
                # row['Na'] = ad_size/
                variables = dict((k, try_convert(row.get(k))) for k in variable_names)
                t_exec_formula = eval(operation["execution_formula"], allowed_funcs, variables)
                t_exec_sim = int(row['Actual Execution Time'])
                t_exec_diff = t_exec_sim - t_exec_formula
                t_latency_sim = int(row['Actual Latency'])
                t_latency_formula = eval(operation["latency_formula"], allowed_funcs, variables)
                t_latency_diff = t_latency_sim - t_latency_formula
                if t_exec_diff > max_diff:
                    max_diff = t_exec_diff
                    max_diff_percent = t_exec_diff * 100 / t_exec_sim
                writer.writerow({**row, t_exec_header: t_exec_formula, t_latency_header: t_latency_formula, exec_diff_header: t_exec_diff, latency_diff_header: t_latency_diff})

            if max_diff > 0:
                logger.warning(
                    f'Maximum discrepancy between formula and simulation time was {max_diff} cycles ({max_diff_percent:.1f}%).')

