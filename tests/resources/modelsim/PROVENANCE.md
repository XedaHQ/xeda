# ModelSim-Intel 2020.1 status excerpt

`status-2020.1.txt` is the exact saved summary of real Docker probes performed on
2026-09-24 for Xeda PR #85, using
`chaseruskin/modelsim-intel:20.1.1-ubuntu-22.04` on `linux/amd64`.
The probes compiled small VHDL/SV units, used `vsim -onfinish stop`, ran them,
and printed `runStatus -full` and
`coverage attribute -name TESTSTATUS -concise`.
The normal/failure VHDL units printed AFTER then executed `std.env.finish`;
the fatal SV unit executed `$fatal`.

This is an excerpt, not a complete vendor transcript. It establishes native
finish/fatal reason and status spelling for that image. It contains no time,
precision, std.env.stop, or Questa measurements. The new runtime/checkpoint
fixtures and fake edition banners are synthetic. The Task 6 implementation
machine had Docker stopped; these older measurements do not constitute fresh
proprietary capability validation.

Native command contracts: Siemens ModelSim/Questa command reference, v2024.2,
`runStatus`, `transcript file`, and `coverage attribute`:
https://ww1.microchip.com/downloads/aemDocuments/documents/FPGA/swdocs/questasim/questa_sim_ref_2024_2.pdf

Native `$now` and `$resolution` formats: ModelSim user's manual, v2024.2,
"Referencing Simulator State Variables":
https://ww1.microchip.com/downloads/aemDocuments/documents/FPGA/swdocs/modelsim/modelsim_user_2024_2.pdf
