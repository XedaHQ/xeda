// pipe_mul: a two-stage pipelined unsigned multiplier.
//
// A stand-in for existing Verilog IP: MulUnit.bsv imports it with `import "BVI"`. It has no
// flow control: a product appears on `out_p`, flagged by `out_valid`, exactly two cycles
// after `in_valid`, whether or not anyone is ready for it.
//
// The synchronous reset follows the convention of bsc's own Verilog library, so that it
// matches the modules bsc generates: active low, or active high when BSV_POSITIVE_RESET is
// defined.
`ifdef BSV_POSITIVE_RESET
  `define BSV_RESET_VALUE 1'b1
`else
  `define BSV_RESET_VALUE 1'b0
`endif

module pipe_mul #(
    parameter WIDTH = 16
) (
    input  wire               CLK,
    input  wire               RST,
    input  wire               in_valid,
    input  wire [WIDTH-1:0]   in_a,
    input  wire [WIDTH-1:0]   in_b,
    output reg                out_valid,
    output reg  [2*WIDTH-1:0] out_p
);
    reg             s1_valid;
    reg [WIDTH-1:0] s1_a;
    reg [WIDTH-1:0] s1_b;

    always @(posedge CLK) begin
        if (RST == `BSV_RESET_VALUE) begin
            s1_valid  <= 1'b0;
            out_valid <= 1'b0;
        end else begin
            s1_valid  <= in_valid;
            out_valid <= s1_valid;
        end
        s1_a  <= in_a;
        s1_b  <= in_b;
        out_p <= {{WIDTH{1'b0}}, s1_a} * {{WIDTH{1'b0}}, s1_b};
    end
endmodule
