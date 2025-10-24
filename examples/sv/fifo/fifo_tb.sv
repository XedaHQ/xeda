`default_nettype none

module fifo_tb;
  timeunit 1ns; timeprecision 1ps;

  localparam int DATA_WIDTH = 8;
  localparam int LOG2_DEPTH = 5;

  localparam int DEPTH = 1 << LOG2_DEPTH;

  logic clk = 0;
  logic reset = 1;

  logic enq_valid, deq_ready, enq_ready, deq_valid;
  logic [DATA_WIDTH-1:0] enq_data, deq_data;


  fifo #(
      .DATA_WIDTH(DATA_WIDTH),
      .LOG2_DEPTH(LOG2_DEPTH)
  ) dut (
      .clk(clk),
      .reset(reset),
      .enq_valid(enq_valid),
      .enq_data(enq_data),
      .enq_ready(enq_ready),
      .deq_ready(deq_ready),
      .deq_data(deq_data),
      .deq_valid(deq_valid)
  );

  always #5 clk = ~clk;

  initial begin
    enq_valid = 0;
    enq_data  = '0;
    deq_ready = 0;
    repeat (5) @(posedge clk);
    reset = 0;
  end

  logic [DATA_WIDTH-1:0] exp_q[$];
  bit writer_done = 0;
  bit full_seen = 0;
  bit empty_seen = 0;

  task automatic push(input logic [DATA_WIDTH-1:0] value);
    int guard = 0;
    enq_data  = value;
    enq_valid = 1;
    do begin
      @(posedge clk);
      guard++;
      if (guard > DEPTH * 8) begin
        $fatal(1, "Timeout waiting for fifo write ready");
      end
    end while (!enq_ready);
    exp_q.push_back(value);
    enq_valid = 0;
    enq_data  = '0;
  endtask

  initial begin : writer
    @(negedge reset);
    push(8'hA5);
    push(8'h5A);
    push(8'h3C);
    push(8'hC3);
    for (int i = 0; i < DEPTH * 1000; i++) begin
      push($urandom());
    end
    writer_done = 1;
  end

  initial begin : reader
    @(negedge reset);
    for (int i = 0; i < DEPTH; i++) begin
      @(posedge clk);
      deq_ready = 0;
    end
    forever begin
      @(posedge clk);
      if (writer_done) begin
        deq_ready = 1;
      end else begin
        deq_ready = ($urandom_range(0, 3) != 0);
      end
    end
  end

  always @(posedge clk) begin
    if (reset) begin
      exp_q.delete();
      full_seen  <= 0;
      empty_seen <= 0;
    end else begin
      if (!enq_ready) full_seen <= 1;
      if (!deq_valid) empty_seen <= 1;
      if (deq_valid && deq_ready) begin
        if (exp_q.size() == 0) begin
          $error("Read observed with empty scoreboard queue");
        end else begin
          logic [DATA_WIDTH-1:0] expected;
          expected = exp_q.pop_front();
          if (deq_data !== expected) begin
            $error("Data mismatch: expected %0h got %0h", expected, deq_data);
          end
        end
      end
    end
  end

  initial begin
    wait (writer_done);
    wait (exp_q.size() == 0 && dut.deq_valid == 0);
    repeat (5) @(posedge clk);
    if (!full_seen) $error("Full condition never observed");
    if (!empty_seen) $error("Empty condition never observed");
    $display("FIFO test completed after %0t ns", $time);
    $finish;
  end

  initial begin
    #2_000_000;
    $fatal(1, "Testbench timeout");
  end

`ifdef DUMP_VCD
  initial begin
    $dumpfile("fifo_tb.vcd");
    $dumpvars(0, fifo_tb);
  end
`endif

endmodule
