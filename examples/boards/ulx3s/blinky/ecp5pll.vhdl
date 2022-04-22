
--===============================================================================================--
--! @file              ecp5pll.vhdl
--! @brief             ECP5 PLL wrapper
--! @author            Kamyar Mohajerani
--! @copyright         Copyright (c) 2022 Kamyar Mohajerani
--!
--! @license           Solderpad Hardware License v2.1 ([SHL-2.1](https://solderpad.org/licenses/SHL-2.1/))
--! @vhdl              VHDL 2008 and later
--!
--! @details          
--!     - Based on SystemVerilog ecp5pll implementation by EMARD:
--!         https://github.com/emard/ulx3s-misc/blob/7413e82/examples/ecp5pll/hdl/sv/ecp5pll.sv
--!     - Actual frequency can be equal or higher than requested
--===============================================================================================--

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ecp5pll is
    generic(
        G_IN_HZ        : positive;
        G_FREQ_SCALE   : positive := 1;
        G_OUT_0_HZ     : positive;
        G_OUT_0_DEG    : natural  := 0; -- keep 0
        G_OUT_0_TOL_HZ : natural  := 0; -- tolerance: if freq differs more, then error
        G_OUT_1_HZ     : natural  := 0;
        G_OUT_1_DEG    : natural  := 0;
        G_OUT_1_TOL_HZ : natural  := 0;
        G_OUT_2_HZ     : natural  := 0;
        G_OUT_2_DEG    : natural  := 0;
        G_OUT_2_TOL_HZ : natural  := 0;
        G_OUT_3_HZ     : natural  := 0;
        G_OUT_3_DEG    : natural  := 0;
        G_OUT_3_TOL_HZ : natural  := 0;
        G_RESET_EN     : boolean  := FALSE;
        G_STANDBY_EN   : boolean  := FALSE;
        G_DYNAMIC_EN   : boolean  := FALSE
    );
    port(
        clk_i        : in  std_logic;
        clk_o_0      : out std_logic;
        clk_o_1      : out std_logic;
        clk_o_2      : out std_logic;
        clk_o_3      : out std_logic;
        reset        : in  std_logic                    := '0';
        standby      : in  std_logic                    := '0';
        phasesel     : in  std_logic_vector(1 downto 0) := (others => '0');
        phasedir     : in  std_logic                    := '0';
        phasestep    : in  std_logic                    := '0';
        phaseloadreg : in  std_logic                    := '0';
        locked       : out std_logic
    );
end entity ecp5pll;

architecture RTL of ecp5pll is
    constant PFD_MIN     : positive := 3125000 / G_FREQ_SCALE;
    constant PFD_MAX     : positive := 400000000 / G_FREQ_SCALE;
    constant VCO_MIN     : positive := 400000000 / G_FREQ_SCALE;
    constant VCO_MAX     : positive := 800000000 / G_FREQ_SCALE;
    constant VCO_OPTIMAL : positive := (VCO_MIN + VCO_MAX) / 2;

    function get_error(out_freq, vco_freq : integer) return integer is
    begin
        if out_freq > 0 then
            if vco_freq >= out_freq then
                return abs (vco_freq / (vco_freq / out_freq) - out_freq);
            else
                return abs (vco_freq - out_freq);
            end if;
        end if;
        return 0;
    end function;

    type T_DIVIDER is record
        refclk, feedback, output : integer;
    end record;

    function GET_DIVS return T_DIVIDER is
        variable input_div_min, input_div_max                     : integer;
        variable output_div_min, output_div_max                   : integer;
        variable feedback_div, feedback_div_min, feedback_div_max : integer;
        variable fvco_tmp, fvco_tmp1, fout_tmp                    : integer;
        variable error, error_prev                                : integer;
        variable ret                                              : T_DIVIDER;
    begin

        fvco_tmp1     := 0;
        error_prev    := 999999999;
        input_div_min := maximum(1, G_IN_HZ / PFD_MAX);
        input_div_max := minimum(128, G_IN_HZ / PFD_MIN);
        for input_div in input_div_min to input_div_max loop
            if (G_OUT_0_HZ / (1000000 / G_FREQ_SCALE) * input_div < 2000) then
                feedback_div := G_OUT_0_HZ * input_div / G_IN_HZ;
            else
                feedback_div := G_OUT_0_HZ / G_IN_HZ * input_div;
            end if;
            feedback_div_min := maximum(1, feedback_div);
            feedback_div_max := minimum(80, feedback_div + 1);
            for feedback_div in feedback_div_min to feedback_div_max loop
                output_div_min := maximum(1, (VCO_MIN / feedback_div) / (G_IN_HZ / input_div));
                output_div_max := minimum(128, (VCO_MAX / feedback_div) / (G_IN_HZ / input_div));
                fout_tmp       := G_IN_HZ * feedback_div / input_div;
                for output_div in output_div_min to output_div_max loop
                    fvco_tmp := fout_tmp * output_div;
                    error    := abs (fout_tmp - G_OUT_0_HZ) + get_error(G_OUT_1_HZ, fvco_tmp) + get_error(G_OUT_2_HZ, fvco_tmp) + get_error(G_OUT_3_HZ, fvco_tmp);
                    if (error < error_prev or (error = error_prev and abs (fvco_tmp - VCO_OPTIMAL) < abs (fvco_tmp1 - VCO_OPTIMAL))) then
                        error_prev := error;
                        ret        := (
                            refclk   => input_div,
                            feedback => feedback_div,
                            output   => output_div
                        );
                        fvco_tmp1  := fvco_tmp;
                    end if;
                end loop;
            end loop;
        end loop;
        return ret;
    end function;

    function F_primary_phase(output_div, deg : integer) return integer is
        variable phase_compensation, phase_count_x8 : integer;
    begin
        phase_compensation := (output_div + 1) / 2 * 8 - 8 + output_div / 2 * 8; -- output_div/2*8 = 180 deg shift
        phase_count_x8     := phase_compensation + 8 * output_div * deg / 360;
        if (phase_count_x8 > 1023) then
            phase_count_x8 := phase_count_x8 mod (output_div * 8); -- wraparound 360 deg
        end if;
        return phase_count_x8;
    end function;

    constant DIVS : T_DIVIDER := GET_DIVS;
    constant FOUT : integer   := G_IN_HZ * DIVS.feedback / DIVS.refclk;
    constant FVCO : integer   := FOUT * DIVS.output;

    constant PRIMARY_CPHASE : integer := F_primary_phase(DIVS.output, G_OUT_0_DEG) / 8;
    constant PRIMARY_FPHASE : integer := F_primary_phase(DIVS.output, G_OUT_0_DEG) mod 8;

    function secondary_divisor(sfreq : integer) return integer is
    begin
        if (sfreq > 0) and (FVCO > sfreq) then
            return FVCO / sfreq;
        end if;
        return 1;
    end function;

    function enabled_str(cond : boolean) return string is
    begin
        if cond then
            return "ENABLED";
        end if;
        return "DISABLED";
    end function;

    function secondary_phase(sfreq, sphase : integer) return integer is
        variable phase_compensation, phase_count_x8 : integer := 0;
        variable div                                : integer := 1;
    begin
        if (sfreq > 0) then
            if (FVCO >= sfreq) then
                div := FVCO / sfreq;
            end if;
            -- freq               := params_fvco / div;
            phase_compensation := div * 8 - 8;
            phase_count_x8     := phase_compensation + 8 * div * sphase / 360;
            if (phase_count_x8 > 1023) then
                phase_count_x8 := phase_count_x8 mod (div * 8); -- wraparound 360 deg
            end if;
        end if;

        return phase_count_x8;
    end function;

    constant SECONDARY_DIV_1    : integer := secondary_divisor(G_OUT_1_HZ);
    constant SECONDARY_CPHASE_1 : integer := secondary_phase(G_OUT_1_HZ, G_OUT_1_DEG) / 8;
    constant SECONDARY_FPHASE_1 : integer := secondary_phase(G_OUT_1_HZ, G_OUT_1_DEG) mod 8;
    constant SECONDARY_DIV_2    : integer := secondary_divisor(G_OUT_2_HZ);
    constant SECONDARY_CPHASE_2 : integer := secondary_phase(G_OUT_2_HZ, G_OUT_2_DEG) / 8;
    constant SECONDARY_FPHASE_2 : integer := secondary_phase(G_OUT_2_HZ, G_OUT_2_DEG) mod 8;
    constant SECONDARY_DIV_3    : integer := secondary_divisor(G_OUT_3_HZ);
    constant SECONDARY_CPHASE_3 : integer := secondary_phase(G_OUT_3_HZ, G_OUT_3_DEG) / 8;
    constant SECONDARY_FPHASE_3 : integer := secondary_phase(G_OUT_3_HZ, G_OUT_3_DEG) mod 8;

    component                           -- attribute: "blackbox"
        EHXPLLL is                      -- @suppress "Component declaration 'EHXPLLL' has none or multiple matching entity declarations"
        generic(
            CLKI_DIV         : integer := 1;
            CLKFB_DIV        : integer := 1;
            CLKOP_DIV        : integer := 8;
            CLKOS_DIV        : integer := 8;
            CLKOS2_DIV       : integer := 8;
            CLKOS3_DIV       : integer := 8;
            CLKOP_ENABLE     : string  := "ENABLED";
            CLKOS_ENABLE     : string  := "DISABLED";
            CLKOS2_ENABLE    : string  := "DISABLED";
            CLKOS3_ENABLE    : string  := "DISABLED";
            CLKOP_CPHASE     : integer := 0;
            CLKOS_CPHASE     : integer := 0;
            CLKOS2_CPHASE    : integer := 0;
            CLKOS3_CPHASE    : integer := 0;
            CLKOP_FPHASE     : integer := 0;
            CLKOS_FPHASE     : integer := 0;
            CLKOS2_FPHASE    : integer := 0;
            CLKOS3_FPHASE    : integer := 0;
            FEEDBK_PATH      : string  := "CLKOP";
            CLKOP_TRIM_POL   : string  := "RISING";
            CLKOP_TRIM_DELAY : integer := 0;
            CLKOS_TRIM_POL   : string  := "RISING";
            CLKOS_TRIM_DELAY : integer := 0;
            OUTDIVIDER_MUXA  : string  := "DIVA";
            OUTDIVIDER_MUXB  : string  := "DIVB";
            OUTDIVIDER_MUXC  : string  := "DIVC";
            OUTDIVIDER_MUXD  : string  := "DIVD";
            PLL_LOCK_MODE    : integer := 0;
            PLL_LOCK_DELAY   : integer := 200;
            STDBY_ENABLE     : string  := "DISABLED";
            REFIN_RESET      : string  := "DISABLED";
            SYNC_ENABLE      : string  := "DISABLED";
            INT_LOCK_STICKY  : string  := "ENABLED";
            DPHASE_SOURCE    : string  := "DISABLED";
            PLLRST_ENA       : string  := "DISABLED";
            INTFB_WAKE       : string  := "DISABLED"
        );
        port(
            CLKI         : in  std_logic;
            CLKFB        : in  std_logic;
            PHASESEL1    : in  std_logic;
            PHASESEL0    : in  std_logic;
            PHASEDIR     : in  std_logic;
            PHASESTEP    : in  std_logic;
            PHASELOADREG : in  std_logic;
            STDBY        : in  std_logic;
            PLLWAKESYNC  : in  std_logic;
            RST          : in  std_logic;
            ENCLKOP      : in  std_logic;
            ENCLKOS      : in  std_logic;
            ENCLKOS2     : in  std_logic;
            ENCLKOS3     : in  std_logic;
            CLKOP        : out std_logic;
            CLKOS        : out std_logic;
            CLKOS2       : out std_logic;
            CLKOS3       : out std_logic;
            LOCK         : out std_logic;
            INTLOCK      : out std_logic;
            REFCLK       : out std_logic;
            CLKINTFB     : out std_logic
        );

    end component;

    attribute ICP_CURRENT            : string;
    attribute LPF_RESISTOR           : string;
    attribute MFG_ENABLE_FILTEROPAMP : string;
    attribute MFG_GMCREF_SEL         : string;
    attribute ICP_CURRENT of PLL_INST : label is "12";
    attribute LPF_RESISTOR of PLL_INST : label is "8";
    attribute MFG_ENABLE_FILTEROPAMP of PLL_INST : label is "1";
    attribute MFG_GMCREF_SEL of PLL_INST : label is "2";

    signal PHASESEL_HW : unsigned(phasesel'range);
begin

    assert abs (G_OUT_0_HZ - FOUT) <= G_OUT_0_TOL_HZ --
    report "cannot generate output frequency for clk_O_0.  G_OUT_0_HZ - params_fout: " & integer'image(G_OUT_0_HZ - FOUT) & " is higer than specified tolerance"
    severity FAILURE;
    assert G_OUT_1_HZ = 0 or abs (G_OUT_1_HZ - FVCO / SECONDARY_DIV_1) <= G_OUT_1_TOL_HZ --
    report "cannot generate output frequency for clk_O_1.  diff: " & integer'image(abs (G_OUT_1_HZ - FVCO / SECONDARY_DIV_1)) & " is higer than specified tolerance"
    severity FAILURE;
    assert G_OUT_2_HZ = 0 or abs (G_OUT_2_HZ - FVCO / SECONDARY_DIV_2) <= G_OUT_2_TOL_HZ --
    report "cannot generate output frequency for clk_O_2.  diff: " & integer'image(abs (G_OUT_2_HZ - FVCO / SECONDARY_DIV_2)) & " is higer than specified tolerance"
    severity FAILURE;
    assert G_OUT_3_HZ = 0 or abs (G_OUT_3_HZ - FVCO / SECONDARY_DIV_3) <= G_OUT_3_TOL_HZ --
    report "cannot generate output frequency for clk_O_3.  diff: " & integer'image(abs (G_OUT_3_HZ - FVCO / SECONDARY_DIV_3)) & " is higer than specified tolerance"
    severity FAILURE;

    PHASESEL_HW <= unsigned(phasesel) - 1;

    assert FALSE report "EHXPLLL parameters:" & --
    LF & "    CLKI_DIV   : " & integer'image(DIVS.refclk) & --
    LF & "    CLKFB_DIV  : " & integer'image(DIVS.feedback) & --
    LF & "    CLKOP_DIV  : " & integer'image(DIVS.output) & --
    LF & "    CLKOS_DIV  : " & integer'image(SECONDARY_DIV_1) & --
    LF & "    CLKOS2_DIV : " & integer'image(SECONDARY_DIV_2) & --
    LF & "    CLKOS3_DIV : " & integer'image(SECONDARY_DIV_3) & --
    LF severity NOTE;

    PLL_INST : EHXPLLL
        generic map(
            CLKI_DIV         => DIVS.refclk,
            CLKFB_DIV        => DIVS.feedback,
            CLKOP_DIV        => DIVS.output,
            CLKOS_DIV        => SECONDARY_DIV_1,
            CLKOS2_DIV       => SECONDARY_DIV_2,
            CLKOS3_DIV       => SECONDARY_DIV_3,
            CLKOP_ENABLE     => "ENABLED",
            CLKOS_ENABLE     => enabled_str(G_OUT_1_HZ > 0),
            CLKOS2_ENABLE    => enabled_str(G_OUT_2_HZ > 0),
            CLKOS3_ENABLE    => enabled_str(G_OUT_3_HZ > 0),
            CLKOP_CPHASE     => PRIMARY_CPHASE,
            CLKOS_CPHASE     => SECONDARY_CPHASE_1,
            CLKOS2_CPHASE    => SECONDARY_CPHASE_2,
            CLKOS3_CPHASE    => SECONDARY_CPHASE_3,
            CLKOP_FPHASE     => PRIMARY_FPHASE,
            CLKOS_FPHASE     => SECONDARY_FPHASE_1,
            CLKOS2_FPHASE    => SECONDARY_FPHASE_2,
            CLKOS3_FPHASE    => SECONDARY_FPHASE_3,
            FEEDBK_PATH      => "CLKOP",
            CLKOP_TRIM_POL   => "RISING",
            CLKOP_TRIM_DELAY => 0,
            CLKOS_TRIM_POL   => "RISING",
            CLKOS_TRIM_DELAY => 0,
            OUTDIVIDER_MUXA  => "DIVA",
            OUTDIVIDER_MUXB  => "DIVB",
            OUTDIVIDER_MUXC  => "DIVC",
            OUTDIVIDER_MUXD  => "DIVD",
            PLL_LOCK_MODE    => 0,
            PLL_LOCK_DELAY   => 200,
            STDBY_ENABLE     => enabled_str(G_STANDBY_EN),
            REFIN_RESET      => "DISABLED",
            SYNC_ENABLE      => "DISABLED",
            INT_LOCK_STICKY  => "ENABLED",
            DPHASE_SOURCE    => enabled_str(G_DYNAMIC_EN),
            PLLRST_ENA       => enabled_str(G_RESET_EN),
            INTFB_WAKE       => "DISABLED"
        )
        port map(
            CLKI         => clk_i,
            CLKFB        => clk_o_0,
            PHASESEL1    => PHASESEL_HW(1),
            PHASESEL0    => PHASESEL_HW(0),
            PHASEDIR     => phasedir,
            PHASESTEP    => phasestep,
            PHASELOADREG => phaseloadreg,
            STDBY        => '0',
            PLLWAKESYNC  => '0',
            RST          => '0',
            ENCLKOP      => '0',
            ENCLKOS      => '0',
            ENCLKOS2     => '0',
            ENCLKOS3     => '0',
            CLKOP        => clk_o_0,
            CLKOS        => clk_o_1,
            CLKOS2       => clk_o_2,
            CLKOS3       => clk_o_3,
            LOCK         => locked,
            INTLOCK      => open,
            REFCLK       => open,
            CLKINTFB     => open
        );

end architecture;
