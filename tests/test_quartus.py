import os
from xeda.flows.quartus import parse_csv, try_num
from . import RESOURCES_DIR


def test_parse_csv():
    resources = parse_csv(
        RESOURCES_DIR / "Fitter_Resource_Utilization_by_Entity.csv",
        id_field='Compilation Hierarchy Node',
        field_parser=lambda s: try_num(s.split()[0]),
        id_parser=lambda s: s.strip().lstrip("|"),
        # interesting_fields=None
        interesting_fields=[
            'Logic Cells',
            'LUT-Only LCs',
            'Register-Only LCs',
            'LUT/Register LCs',
            'Dedicated Logic Registers',
            'ALMs needed [=A-B+C]',
            'Combinational ALUTs',
            'ALMs used for memory',
            'Memory Bits', 'M10Ks', 'M9Ks', 'DSP Elements',
            'DSP Blocks',
            'Block Memory Bits',
            'Pins',
            'I/O Registers',
        ]
        # ['Logic Cells', 'Memory Bits', 'M10Ks', 'M9Ks', 'DSP Elements', 'ALMs needed [=A-B+C]',
        #                     'Combinational ALUTs', 'ALMs used for memory', 'DSP Blocks', 'Pins'
        #                     'LUT-Only LCs',	'Register-Only LCs', 'LUT/Register LCs', 'Block Memory Bits']
    )
    assert resources == {
        'full_adder_piped': {
            'ALMs needed [=A-B+C]': 1.5,
            'ALMs used for memory': 0.0,
            'Block Memory Bits': 0,
            'Combinational ALUTs': 3,
            # 'Compilation Hierarchy Node': '|full_adder_piped',
            'DSP Blocks': 0,
            'Dedicated Logic Registers': 2,
            # 'Entity Name': 'full_adder_piped',
            # 'Full Hierarchy Name': '|full_adder_piped',
            'I/O Registers': 0,
            # 'Library Name': 'work',
            'M10Ks': 0,
            'Pins': 7,
            # 'Virtual Pins': 0,
            # '[A] ALMs used in final placement': 1.5,
            # '[B] Estimate of ALMs recoverable by dense packing': 0.0,
            # '[C] Estimate of ALMs unavailable': 0.0
        },
    }


def test_parse_csv_no_header():
    parsed = parse_csv(RESOURCES_DIR / "Flow_Summary.csv", None)
    assert parsed == {
        'Flow Status': 'Successful - Tue Mar  1 11:10:35 2022',
        'Quartus Prime Version': '21.1.0 Build 842 10/21/2021 SJ Lite Edition',
        'Revision Name': 'pipelined_adder',
        'Top-level Entity Name': 'full_adder_piped',
        'Family': 'Cyclone V',
        'Device': '5CGXBC3B6F23C7', 'Timing Models': 'Final',
        'Total registers': '2',
        'Total pins': '7 / 222 ( 3 % )',
        'Total virtual pins': '0',
        'Total DSP Blocks': '0 / 57 ( 0 % )',
        'Total HSSI RX PCSs': '0 / 3 ( 0 % )',
        'Total HSSI PMA RX Deserializers': '0 / 3 ( 0 % )',
        'Total HSSI TX PCSs': '0 / 3 ( 0 % )',
        'Total HSSI PMA TX Serializers': '0 / 3 ( 0 % )',
        'Total PLLs': '0 / 7 ( 0 % )', 'Total DLLs': '0 / 3 ( 0 % )'
    }


# Python program to explain os.cpu_count() method

# importing os module


# Get the number of CPUs
# in the system using
# os.cpu_count() method
cpuCount = os.cpu_count()

# Print the number of
# CPUs in the system
print("Number of CPUs in the system:", cpuCount)
