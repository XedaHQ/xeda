// A second root module that dumps a cocotb simulation, which has no testbench of its own to do it.
module iverilog_dump;
  initial begin
    $dumpfile({{ dump_file }});
    $dumpvars(0, {{ top }});
  end
endmodule
