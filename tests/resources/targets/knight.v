module knight #(parameter WIDTH = 8) (input CLK, output [WIDTH-1:0] led);
  assign led = {WIDTH{CLK}};
endmodule
