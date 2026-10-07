module blinky (
    input  wire       clk,
    input  wire       rst,
    output wire [3:0] led
);
  reg [26:0] count = 27'd0;
  always @(posedge clk) begin
    if (rst) count <= 27'd0;
    else count <= count + 27'd1;
  end
  assign led = count[26:23];
endmodule
