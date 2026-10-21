# Basys-3 pins for the board-agnostic top (ports CLK, rxd, txd, led[3:0], sw[3:0], btn).
# Pins and standards are from src/xeda/data/boards/basys3/board.xdc (clock, UART, LEDs, center
# button) and the Digilent Basys-3 master XDC (switches). Only the port names differ.
# No create_clock: the flow's clock setting writes it. Pins only, one set_property per port.

## Clock, 100 MHz oscillator
set_property PACKAGE_PIN W5 [get_ports {CLK}]
set_property IOSTANDARD LVCMOS33 [get_ports {CLK}]

## USB-UART (FT2232HQ). The FPGA receives on B18 (RsRx) and transmits on A18 (RsTx).
set_property PACKAGE_PIN B18 [get_ports {rxd}]
set_property IOSTANDARD LVCMOS33 [get_ports {rxd}]
set_property PACKAGE_PIN A18 [get_ports {txd}]
set_property IOSTANDARD LVCMOS33 [get_ports {txd}]

## LEDs led[0] to led[3]
set_property PACKAGE_PIN U16 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[0]}]
set_property PACKAGE_PIN E19 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[1]}]
set_property PACKAGE_PIN U19 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[2]}]
set_property PACKAGE_PIN V19 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[3]}]

## Switches sw[0] to sw[3]
set_property PACKAGE_PIN V17 [get_ports {sw[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[0]}]
set_property PACKAGE_PIN V16 [get_ports {sw[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[1]}]
set_property PACKAGE_PIN W16 [get_ports {sw[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[2]}]
set_property PACKAGE_PIN W17 [get_ports {sw[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[3]}]

## Center button
set_property PACKAGE_PIN U18 [get_ports {btn}]
set_property IOSTANDARD LVCMOS33 [get_ports {btn}]
