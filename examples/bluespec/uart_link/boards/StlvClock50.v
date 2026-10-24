// Kintex-7 clocking for the STLV7325 v2 200 MHz differential clock input.
module StlvClock50(input wire CLK_P, input wire CLK_N, output wire CLK_50M);
    wire clk_ibuf;
    wire clk_in;
    wire clk_fb;
    wire clk_mmcm;
    wire locked;

    IBUFDS #(
        .DIFF_TERM("TRUE"),
        .IBUF_LOW_PWR("FALSE")
    ) refclk_ibuf (
        .I(CLK_P),
        .IB(CLK_N),
        .O(clk_ibuf)
    );
    BUFG input_bufg (
        .I(clk_ibuf),
        .O(clk_in)
    );

    MMCME2_ADV #(
        .BANDWIDTH("OPTIMIZED"),
        .CLKFBOUT_MULT_F(4.0),
        .CLKIN1_PERIOD(5.0),
        .CLKOUT0_DIVIDE_F(16.0),
        .CLKOUT0_PHASE(0.0),
        .DIVCLK_DIVIDE(1),
        .REF_JITTER1(0.01),
        .STARTUP_WAIT("FALSE")
    ) core_mmcm (
        .CLKIN1(clk_in),
        .CLKIN2(1'b0),
        .CLKINSEL(1'b1),
        .CLKFBIN(clk_fb),
        .CLKFBOUT(clk_fb),
        .CLKOUT0(clk_mmcm),
        .CLKOUT1(), .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(), .CLKOUT6(),
        .DADDR(7'b0), .DCLK(1'b0), .DEN(1'b0), .DI(16'b0), .DWE(1'b0), .DO(), .DRDY(),
        .PSCLK(1'b0), .PSEN(1'b0), .PSINCDEC(1'b0),
        .PWRDWN(1'b0), .RST(1'b0), .LOCKED(locked)
    );

    BUFGCE #(
        .CE_TYPE("SYNC"),
        .IS_CE_INVERTED(1'b0),
        .IS_I_INVERTED(1'b0)
    ) core_bufg (
        .CE(locked),
        .I(clk_mmcm),
        .O(CLK_50M)
    );
endmodule
