# XEDA: Simplified X-target X-tool X-HDL Simulation and Synthesis Framework

A simuplified automation for simulation and synthesis of hardware designs. Simplifying use of commercial and open-source EDA tools, targeting FPGA devices and ASIC fabrication.


## Description

## Definitions
- Tool
- Suite
- Flow
- Dependencies

Supported Tools:
- Xilinx Vivado
- Lattice Diamond Suite

## Installation

### Dependencies


## Usage

## Configurations

Settings override in the following order:
- System-wide `default.json` in `<DATA_DIR>/config/xeda/defaults.json`
- Design-specific `desgin.json`
- Command-line options

Sample `design.json`:

```json
{
    "design": {
        "name": "xoodyak",
        "description": "Xoodyak NIST LWC Candidate Hardware Implementation",
        "author":[ "Xoodyak Team", "[Silvia Mella](mailto:silvia.mella@st.com)"],
        "url": "https://github.com/KeccakTeam/Xoodoo.git",
        "sources": [
            {
                "file": "src_rtl/design_pkg.vhd"
            },
            {
                "file": "src_rtl/LWC/NIST_LWAPI_pkg.vhd"
            },
            {
                "file": "src_rtl/xoodoo_globals.vhd"
            },
            {
                "file": "src_rtl/xoodoo_rc.vhd"
            },
            {
                "file": "src_rtl/xoodoo_register.vhd"
            },
            {
                "file": "src_rtl/xoodoo_round.vhd"
            },
            {
                "file": "src_rtl/xoodoo_1rnd.vhd"
            },
            {
                "file": "src_rtl/xoodoo_n_rounds.vhd"
            },
            {
                "file": "src_rtl/CryptoCore.vhd"
            },
            {
                "file": "src_rtl/LWC/StepDownCountLd.vhd"
            },
            {
                "file": "src_rtl/LWC/data_sipo.vhd"
            },
            {
                "file": "src_rtl/LWC/key_piso.vhd"
            },
            {
                "file": "src_rtl/LWC/data_piso.vhd"
            },
            {
                "file": "src_rtl/LWC/fwft_fifo.vhd"
            },
            {
                "file": "src_rtl/LWC/PreProcessor.vhd"
            },
            {
                "file": "src_rtl/LWC/PostProcessor.vhd"
            },
            {
                "file": "src_rtl/LWC/LWC.vhd"
            },
            {
                "file": "src_rtl/LWC/std_logic_1164_additions.vhd",
                "sim_only": true
            },
            {
                "file": "src_tb/LWC_TB.vhd",
                "sim_only": true
            }
        ],
        "vhdl_std": "02",
        "top": "LWC",
        "clock_port": "clk",
        "tb_top": "LWC_TB",
        "tb_generics": {
            "G_FNAME_PDI": {
                "file": "KAT/v1/pdi.txt"
            },
            "G_FNAME_SDI": {
                "file": "KAT/v1/sdi.txt"
            },
            "G_FNAME_DO": {
                "file": "KAT/v1/do.txt"
            }
        },
        "generics": {}
    },
    "flows": {
        "diamond": {
            "fpga_part": "LFE5U-25F-6BG381C",
            "clock_period": 11.061946902654867,
            "synthesis_engine": "synplify",
            "strategy": "Timing"
        },
        "vivado": {
            "fpga_part": "xc7a12tcsg325-3",
            "clock_period": 4.369,
            "strategy": "Timing",
            "optimize_power": "False",
            "sim_run": "all"
        }
    }
}
```

### Supported Flows

### Adding new Flows

© 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)