
module blinky(
    input  clk_25mhz,
    input  [6:0] btn,
    output [7:0] led
);

    localparam ctr_width = 32;
    localparam ctr_lsb = 20;

    wire clk;
    assign clk = clk_25mhz;

    // __GEN_ECP5_PLL gen_pll_inst(.in_clk(clk_25mhz), .out_clk(pll_clk0), .locked(pll_locked));


    reg [ctr_width-1:0] ctr;

    wire [2:0] cx;
    wire [2:0] shift_val;

    assign cx = ctr[ctr_lsb + 2:ctr_lsb];
    assign shift_val = ctr[ctr_lsb + 3] ? (7 - cx) : cx;

    always @(posedge clk) begin
        ctr <= ctr + 1;
        led <= 1 << shift_val;
    end

endmodule
