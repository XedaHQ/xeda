// macram: an inferred 1K x 16 block RAM with initial contents and a 16 x 16 multiply-accumulate,
// so that a netlist carries a block RAM, a DSP block and carry chains through Vivado. The pins of
// a Basys 3 (xc7a35tcpg236-1) are in macram.xdc.
module macram (
    input  wire        clk,
    input  wire        rst,
    input  wire [15:0] sw,
    output reg  [15:0] led = 16'd0
);
  reg [15:0] mem [0:1023];
  integer i;
  initial for (i = 0; i < 1024; i = i + 1) mem[i] = i * 37;

  reg [9:0]  waddr = 10'd0;
  reg [9:0]  raddr = 10'd0;
  reg [15:0] rdata = 16'd0;
  always @(posedge clk) begin
    if (rst) begin
      waddr <= 10'd0;
      raddr <= 10'd0;
    end else begin
      waddr <= waddr + 10'd1;
      raddr <= waddr ^ 10'h200;  // never equal to the next write address: no collision
      mem[waddr] <= sw ^ {6'd0, waddr};
    end
    rdata <= mem[raddr];
  end

  reg signed [15:0] a_r = 16'sd0;
  reg signed [15:0] b_r = 16'sd0;
  reg signed [31:0] p_r = 32'sd0;
  reg signed [47:0] acc = 48'sd0;
  always @(posedge clk) begin
    a_r <= rdata;
    b_r <= sw;
    p_r <= a_r * b_r;
    if (rst) acc <= 48'sd0;
    else acc <= acc + p_r;
    led <= acc[47:32] ^ acc[15:0];
  end
endmodule
