# ASASSYN
A Simplified Automation for Simulation and SYNthesis

## Description

## Definitions
- Tool
- Suite
- Flow
- Dependencies

## Installation

### Dependencies


## Usage

## Configurations

Sample `design.json`:

```json
{
    "design": {
        "name": "xoodyak",
        "sources": [
            "src_rtl/design_pkg.vhd",
            "src_rtl/LWC/NIST_LWAPI_pkg.vhd",
            "src_rtl/xoodoo_globals.vhd",
            "src_rtl/xoodoo_rc.vhd",
            "src_rtl/xoodoo_register.vhd",
            "src_rtl/xoodoo_round.vhd",
            "src_rtl/xoodoo_1rnd.vhd",
            "src_rtl/xoodoo_n_rounds.vhd",
            "src_rtl/CryptoCore.vhd",
            "src_rtl/LWC/StepDownCountLd.vhd",
            "src_rtl/LWC/data_sipo.vhd",
            "src_rtl/LWC/key_piso.vhd",
            "src_rtl/LWC/data_piso.vhd",
            "src_rtl/LWC/fwft_fifo.vhd",
            "src_rtl/LWC/PreProcessor.vhd",
            "src_rtl/LWC/PostProcessor.vhd",
            "src_rtl/LWC/LWC.vhd"
        ],
        "vhdl_std": "02",
        "top": "LWC",
        "clock_port": "clk",
        "tb_sources": [
            "src_rtl/LWC/std_logic_1164_additions.vhd",
            "src_tb/LWC_TB.vhd"
        ],
        "tb_top": "LWC_TB",
        "tb_generics": {
            "G_FNAME_PDI": {"path": "KAT/v1/pdi.txt"},
            "G_FNAME_SDI": {"path": "KAT/v1/sdi.txt"},
            "G_FNAME_DO": {"path": "KAT/v1/do.txt"}
        },
        "generics": {}
    },
    "flows": {
        "diamond": {
            "fpga_part": "LFE5U-25F-6BG381C",
            "clock_period": 11.548,
            "synthesis_engine": "synplify",
            "strategy": "Timing"
        },
        "vivado": {
            "fpga_part": "xc7a12tcsg325-3",
            "clock_period": 4.2,
            "strategy": "Timing"
        }
    }
}
```

### Supported Flows

### Adding new Flows

© 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)