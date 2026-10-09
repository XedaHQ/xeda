`timescale 1ns / 1ps
// Drives macram with a 16-bit LFSR on `sw` and prints `led` once per cycle from cycle 40 on.
// Everything happens at the falling edge, so a zero-delay netlist simulation samples the same
// values as the RTL. `rst` is held for 30 cycles (300 ns), longer than the 100 ns global
// set/reset pulse of Xilinx simulation models.
module tb_macram;
  reg clk = 1'b0;
  reg rst = 1'b1;
  reg [15:0] sw = 16'hACE1;
  wire [15:0] led;
  integer cycle = 0;

  macram dut (.clk(clk), .rst(rst), .sw(sw), .led(led));

  always #5 clk = ~clk;

  always @(negedge clk) begin
    if (cycle >= 40) $display("%0d %h", cycle, led);
    if (cycle == 2000) $finish;
    sw <= {sw[14:0], sw[15] ^ sw[13] ^ sw[12] ^ sw[10]};
    if (cycle == 30) rst <= 1'b0;
    cycle = cycle + 1;
  end
endmodule
