module blink(
    input clk,
    input reset,
    output led
);
    logic [7:0] counter;

    always @(posedge clk) begin
        if (reset == 1)
            counter <= 0;
        else
            counter <= counter + 1'b1;
    end

    assign led = counter[7];

endmodule
