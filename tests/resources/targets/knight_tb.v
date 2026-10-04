module knight_tb;
  reg clk = 0;
  knight dut (.CLK(clk));
  initial #10 $finish;
endmodule
