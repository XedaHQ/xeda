--===============================================================================================--
--! @file              blinky.vhdl
--! @brief             
--! @author            Kamyar Mohajerani
--! @copyright         Copyright (c) 2022 Kamyar Mohajerani
--!
--! @license           Solderpad Hardware License v2.1 ([SHL-2.1](https://solderpad.org/licenses/SHL-2.1/))
--! @vhdl              VHDL 2008 and later
--!
--! @details           
--!
--===============================================================================================--

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package utils_pkg is
    function log2ceil(n : natural) return natural;
    type T_UNSIGNED_ARRAY is array (natural range <>) of unsigned;
end package;

package body utils_pkg is
    --====================================== Functions
    --! Returns the number of bits required to represet values less than n (0 to n - 1 inclusive)
    function log2ceil(n : natural) return natural is
        variable r : natural := 0;
    begin
        while n > 2 ** r loop
            r := r + 1;
        end loop;
        return r;
    end function;

end package body;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.utils_pkg.all;

entity debouncer is
    generic(
        G_W        : positive;
        G_N_FLOPS  : positive := 2;
        G_CTR_BITS : positive := log2ceil(25_000_000 * 10 / 1000) -- freq(Hz) * stable_time(ms)
    );
    port(
        clk      : in  std_logic;
        rst      : in  std_logic := '0';
        in_bits  : in  std_logic_vector(G_W - 1 downto 0);
        out_bits : out std_logic_vector(G_W - 1 downto 0)
    );
end entity debouncer;

architecture RTL of debouncer is
    -- registers:
    signal debounce_ctr : T_UNSIGNED_ARRAY(0 to G_W - 1)(G_CTR_BITS - 1 downto 0);
    signal flops        : T_UNSIGNED_ARRAY(0 to G_W - 1)(G_N_FLOPS - 1 downto 0) := (others => (others => '0')); -- init for FPGA impl
    signal out_bits_reg : std_logic_vector(G_W - 1 downto 0);
begin
    out_bits <= out_bits_reg;

    process(clk) is
    begin
        if rising_edge(clk) then
            if rst then
                flops        <= (others => (others => '0'));
                out_bits_reg <= (others => '0');
            else
                for i in 0 to G_W - 1 loop
                    flops(i) <= in_bits(i) & flops(i)(G_N_FLOPS - 1 downto 1);
                    if (or flops(i)) and (nand flops(i)) then -- flops content are different (bounce)
                        debounce_ctr(i) <= (others => '0');
                    elsif debounce_ctr(i)(G_CTR_BITS - 1) = '0' then -- stable and not enough time
                        debounce_ctr(i) <= debounce_ctr(i) + 1;
                    else
                        out_bits_reg(i) <= flops(i)(0); -- stable and held long enough
                    end if;
                end loop;
            end if;
        end if;
    end process;
end architecture;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.utils_pkg.log2ceil;

entity blinky is
    port(
        -- 25MHz clock
        clk_25mhz : in    std_logic;
        -- Buttons
        btn       : in    std_logic_vector(6 downto 0);
        -- LEDs
        led       : out   std_logic_vector(7 downto 0);
        -- GPIO
        gp        : inout std_logic_vector(27 downto 0) := (others => 'Z');
        gn        : inout std_logic_vector(27 downto 0) := (others => 'Z')
    );
end entity blinky;

architecture RTL of blinky is

    --====================================== Constants
    constant MAX_SLOWDOWN          : natural  := 20;
    constant MIN_SLOWDOWN          : natural  := 14;
    constant SPEED_CHANGE_SLOWDOWN : natural  := 18;
    constant LOG2_LED_BITS         : positive := log2ceil(led'length);

    --====================================== Registers
    signal counter       : unsigned(MAX_SLOWDOWN + LOG2_LED_BITS downto 0);
    signal speed_counter : unsigned(log2ceil(MAX_SLOWDOWN - MIN_SLOWDOWN) + SPEED_CHANGE_SLOWDOWN - 1 downto 0);

    --====================================== Wires
    signal clk           : std_logic;
    signal shift_val     : unsigned(LOG2_LED_BITS - 1 downto 0);
    signal counter_slice : unsigned(LOG2_LED_BITS downto 0);
    signal buttons       : std_logic_vector(1 downto 0);

    --====================================== Aliases
    alias counter_slice_lo : unsigned is counter_slice(LOG2_LED_BITS - 1 downto 0);
    alias counter_slice_hi : std_logic is counter_slice(LOG2_LED_BITS);
    alias counter_hi       : unsigned is counter(counter'high downto MIN_SLOWDOWN);
    alias speed            : unsigned is speed_counter(speed_counter'high downto SPEED_CHANGE_SLOWDOWN);
begin

    INST_PLL : entity work.ecp5pll
        generic map(
            G_IN_HZ    => 25_000_000,
            G_OUT_0_HZ => 3_125_000,
            G_RESET_EN => FALSE
        )
        port map(
            clk_i        => clk_25mhz,
            clk_o_0      => clk,
            reset        => '0',
            standby      => '0',
            phasesel     => (others => '0'),
            phasedir     => '0',
            phasestep    => '0',
            phaseloadreg => '0',
            locked       => open
        );
    -- select a 4 bit slice of `counter`. Lowest bit index is determined by index
    counter_slice <= shift_right(counter_hi, to_integer(speed))(counter_slice'range);
    shift_val     <= led'length - 1 - counter_slice_lo when counter_slice_hi else counter_slice_lo;

    INST_DEBOUNCER : entity work.debouncer
        generic map(
            G_W        => buttons'length,
            G_N_FLOPS  => 3,
            G_CTR_BITS => 14
        )
        port map(
            clk      => clk,
            in_bits  => (btn(6), btn(5)),
            out_bits => buttons
        );

    process(clk) is
    begin
        if rising_edge(clk) then
            counter <= counter + 1;
            led     <= std_logic_vector(shift_left(to_unsigned(1, led'length), to_integer(shift_val)));
            if buttons(0) then
                if nand speed then
                    speed_counter <= speed_counter + 1; -- saturate high (speed == 11...1)
                end if;
            elsif buttons(1) then
                if or speed then
                    speed_counter <= speed_counter - 1; -- saturate low (speed == 0)
                end if;
            end if;
        end if;
    end process;
end architecture;
