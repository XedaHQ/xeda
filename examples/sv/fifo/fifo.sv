`default_nettype none

module fifo #(
    parameter DATA_WIDTH = 8,
    parameter LOG2_DEPTH = 4
) (
    input logic clk,
    input logic reset,
    input logic enq_valid,
    input logic [DATA_WIDTH-1:0] enq_data,
    output logic enq_ready,
    input logic deq_ready,
    output logic [DATA_WIDTH-1:0] deq_data,
    output logic deq_valid
);
  logic [DATA_WIDTH-1:0] mem[2**LOG2_DEPTH];
  logic [LOG2_DEPTH-1:0] write_ptr, read_ptr;
  logic maybe_full, overlap, do_write, do_read;

  assign overlap   = write_ptr == read_ptr;
  assign do_write  = enq_valid && enq_ready;
  assign do_read   = deq_valid && deq_ready;

  assign enq_ready = !(overlap && maybe_full);
  assign deq_valid = maybe_full || !overlap;
  assign deq_data  = mem[read_ptr];


  always_ff @(posedge clk) begin
    if (reset) begin
      write_ptr  <= '0;
      read_ptr   <= '0;
      maybe_full <= 1'b0;
    end else begin
      if (do_write) begin
        mem[write_ptr] <= enq_data;
        write_ptr <= write_ptr + 1;
        maybe_full <= 1'b1;
      end
      if (do_read) begin
        read_ptr   <= read_ptr + 1;
        maybe_full <= 1'b0;
      end
    end
  end

endmodule
