from copy import deepcopy
import logging
import os
import re
from types import SimpleNamespace
from typing import List

from xeda.flows.flow import FileResource, Flow
from xeda.flows.settings import Settings
from xeda.flows.vivado.vivado_sim import VivadoSim

from ..lwc import LWC

__all__ = ['VivadoSimTiming']

_logger = logging.getLogger()


# similar to str.removesuffix in Python 3.9+
def removesuffix(s: str, suffix: str) -> str:
    return s[:-len(suffix)] if suffix and s.endswith(suffix) else s


class VivadoSimTiming(VivadoSim, LWC):
    def __init__(self, settings: Settings, args: SimpleNamespace, completed_dependencies: List['Flow']):
        tb_settings = settings.design.get('tb', {})

        lwc_settings = settings.design.get('lwc', {})

        tb_settings['top'] = 'LWC_TB'

        if not lwc_settings.get('two_pass'):
            tb_settings['configuration_specification'] = None
        elif tb_settings.get('configuration_specification'):
            _logger.warning("Two-pass LWC. Not overwriting top configuration!")
            _logger.warning(f"design.tb.configuration_specification={tb_settings['configuration_specification']}")

        tb_generics = tb_settings.get('generics', {})
        tb_generics['G_MAX_FAILURES'] = 1
        tb_generics['G_TEST_MODE'] = 4

        tb_settings['generics'] = tb_generics  # in case they didn't exist
        settings.design['tb'] = tb_settings

        tv_root = 'KAT'
        variant = LWC.variant(settings.design)

        for t in ['pdi', 'sdi', 'do']:
            gen = f'G_FNAME_{t.upper()}'
            if gen in tb_generics:
                del tb_generics[gen]

        tvs = ['generic_aead_sizes_new_key']

        if LWC.supports_hash(settings.design):
            tvs.extend(['basic_hash_sizes'])

        run_configs = []

        for tv_subfolder in tvs:
            rc_generics = deepcopy(tb_generics)
            rc_generics['G_FNAME_TIMING'] = f'LWC_TB_timing_{tv_subfolder}.log'
            rc_generics['G_FNAME_LOG'] = f'LWC_TB_log_{tv_subfolder}.log'

            for t in ['pdi', 'sdi', 'do']:
                rc_generics[f'G_FNAME_{t.upper()}'] = FileResource(
                    os.path.join(tv_root, variant, tv_subfolder, f'{t}.txt'))

            run_configs.append(dict(generics=rc_generics, name=tv_subfolder))

        settings.flow['run_configs'] = run_configs

        super().__init__(settings, args, completed_dependencies)

    def parse_reports(self):
        VivadoSim.parse_reports(self)
        results = self.results

        if not results.get('success'):
            results['success'] = False
            return

        tb_settings = self.settings.design['tb']

        run_configs = self.settings.flow.get('run_configs', [dict(
            generics=tb_settings.get('generics', {}), name="DEFAULT")])

        success_pat = re.compile(
            r"PASS \(0\): SIMULATION FINISHED after (?P<cycles>\d+) cycles at (?P<totaltime>.*)")
        pdi_pat = re.compile(
            r"####\s+(?P<op>\w+.*)\s*\n\s*#### MsgID\s*=\s*(?P<msgid>\d+)\s*,?\s*KeyID\s*=\s*(?P<keyid>\d+)\s*,?\s*(?P<the_rest>\w+.*)\s*\n", re.MULTILINE | re.IGNORECASE)

        timing_results = {}

        for rc in run_configs:
            rc_results = {}
            rc_generics = rc['generics']
            rc_name = rc['name']

            lwctb_log = self.flow_run_dir / rc_generics['G_FNAME_LOG']
            with open(lwctb_log) as f:
                match = success_pat.search(f.read())
                if not match:
                    _logger.critical(
                        f"timing pattern not found in the LWC_TB log {lwctb_log}. Make sure simulation has not failed and that you are using the correct version of LWC_TB")
                    self.results['success'] = False
                    return
            rc_results['postreset_cycles'] = match.group('cycles')
            rc_results['total_sim_time'] = match.group('totaltime')

            timing_txt = self.flow_run_dir / rc_generics['G_FNAME_TIMING']
            with open(timing_txt) as f:
                timings = f.readlines()
                if not timings:
                    _logger.critical(f"Timings file {timing_txt} is empty!")
                    self.results['success'] = False
                    return

            pdi: FileResource = rc_generics[f'G_FNAME_PDI']
            sdi = rc_generics.get(f'G_FNAME_SDI')
            do = rc_generics[f'G_FNAME_DO']

            with open(pdi.file) as f:
                content = f.read()

            def short_op(opstr):
                if opstr == 'Authenticated Encryption':
                    return 'Encrypt'
                if opstr == 'Authenticated Decryption':
                    return 'Decrypt'
                return opstr

            msgs = []
            for match in pdi_pat.finditer(content):
                op = short_op(match.group('op'))
                d = dict(op=op, msgid=match.group('msgid'),
                         keyid=match.group('keyid'))
                the_rest = match.group('the_rest')
                the_rest = re.split(r'\s*,\s*', the_rest)
                for x in the_rest:
                    x = re.split(r'\s*=\s*', x.strip())
                    d[x[0]] = int(x[1])
                msgs.append(d)

            rc_results['PDI'] = str(pdi)
            rc_results['SDI'] = str(sdi)
            rc_results['DO'] = str(do)

            tp = []
            for t in timings:
                t = t.strip()
                p = re.split(f'\s*,\s*', t)
                assert len(p) == 2, "should be pairs of msgid, cycles"
                tp.append(tuple(p))

            assert len(msgs) == len(
                tp), f"count of messages in {pdi} and timings from {timing_txt} do not match!"

            for msg, (id, cycles) in zip(msgs, tp):
                assert msg['msgid'] == id
                op = msg['op']
                msg = {removesuffix(k, 'Size').strip().upper(
                ): v for k, v in msg.items() if k not in ['keyid', 'op', 'msgid']}

                timing_results[op] = timing_results.get(
                    op, []) + [(msg, cycles)]

            results['TV:' + rc_name] = rc_results

        supports_hash = LWC.supports_hash(self.settings.design)

        lwc_settings = self.settings.design.get('lwc', {})
        block_size_bits = lwc_settings.get('block_bits')
        assert 'AD' in block_size_bits
        assert 'PT' in block_size_bits
        if supports_hash:
            assert 'HM' in block_size_bits

        block_size_bits['CT'] = block_size_bits['PT']
        sizes = [16, 64, 1536]

        csv_lines = []

        bsx4 = None
        bsx5 = None

        # enc, dec
        for op in ['Encrypt', 'Decrypt']:
            row = {}
            tr = timing_results.get(op)
            if not tr:
                continue
            xt = 'PT' if op == 'Encrypt' else 'CT'
            # Cryptic code ahead
            for msg_type in ['AD', xt, None]:
                msg_type = (msg_type,) if msg_type else ('AD', xt)
                bsizes = [block_size_bits[m] for m in msg_type]
                xxz = [tuple(sz for _ in bsizes) for sz in sizes] + \
                    [tuple(bs * j // 8 for bs in bsizes) for j in range(4, 6)]
                for idx,sz in enumerate(xxz):
                    for msg, cycle in tr:
                        # print(list(zip(msg_type,sz)))
                        right_value = all(z == msg.get(m)
                                          for m, z in zip(msg_type, sz))
                        others_zero = all(
                            v == 0 for k, v in msg.items() if k not in msg_type)
                        if right_value and others_zero:
                            row[f'{"+".join(msg_type)}_{sz[0] if len(sz) == 1 or (sz[0] == sz[1]) else f"{sz[0]}+{sz[1]}"}'] = cycle
                            print(idx)
                            if idx == 3:
                                bsx4 = cycle
                            elif idx == 4:
                                bsx5  = cycle
                if bsx4 and bsx5:
                    row[f'{"+".join(msg_type)}_Long'] = str(int(bsx5) - int(bsx4))
                
            csv_lines.append(op)
            csv_lines.append(', '.join(row.keys()))
            csv_lines.append(', '.join(row.values()))
            csv_lines.append("")

        if supports_hash:
            op = 'Hash'
            bsx4 = None
            bsx5 = None
            row = {}
            hm_sizes = sizes + [block_size_bits['HM'] * j // 8 for j in range(4, 6)]
            for idx,sz in enumerate(hm_sizes):
                for msg, cycle in timing_results[op]:
                    if msg.get('HM') == sz:
                        row[f'HM_{sz}'] = cycle
                        if idx == 3:
                            bsx4 = cycle
                        elif idx == 4:
                            bsx5  = cycle
            if bsx4 and bsx5:
                row[f'HM_Long'] = str(int(bsx5) - int(bsx4))

            csv_lines.append(op)
            csv_lines.append(', '.join(row.keys()))
            csv_lines.append(', '.join(row.values()))
            csv_lines.append("")

        name = self.settings.design.get('name', "<NO-NAME>")
        results_dir = self.xeda_run_dir / 'Results'
        results_dir.mkdir(exist_ok=True, parents=True)
        csv_files = results_dir / (name + '_Timing.csv')
        with open(csv_files, 'w') as f:
            f.writelines([l + '\n' for l in csv_lines])

        _logger.info(f"Timing results written to {csv_files}")

        results.update(timing_results)

        results['success'] = True
