--------------------------------------------------------------------------------
-- Engineer:    Mike Field <hamster@snap.net.nz>
-- Description: Converts VGA signals into DVID bitstreams.
--
--    'clk_shift' 10x clk_pixel for SDR
--    'clk_shift'  5x clk_pixel for DDR
--
--    'blank' should be asserted during the non-display 
--    portions of the frame
--------------------------------------------------------------------------------
-- See: http://hamsterworks.co.nz/mediawiki/index.php/Dvid_test
--      http://hamsterworks.co.nz/mediawiki/index.php/FPGA_Projects
--
-- Copyright (c) 2012 Mike Field <hamster@snap.net.nz>
--
-- Permission is hereby granted, free of charge, to any person obtaining a copy
-- of this software and associated documentation files (the "Software"), to deal
-- in the Software without restriction, including without limitation the rights
-- to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
-- copies of the Software, and to permit persons to whom the Software is
-- furnished to do so, subject to the following conditions:
--
-- The above copyright notice and this permission notice shall be included in
-- all copies or substantial portions of the Software.
--
-- THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
-- IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
-- FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
-- AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
-- LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
-- OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
-- THE SOFTWARE.
--

-- takes VGA input and prepares output

-- for SDR buffer, which send 1 bit per 1 clock period output out_red(0), out_green(0), ... etc.
-- for DDR buffers, which send 2 bits per 1 clock period output out_red(1 downto 0), ...

-- EMARD unified SDR and DDR into one module

library ieee;
use ieee.std_logic_1164.all;
use IEEE.numeric_std.ALL;

entity vga2dvid is
    Generic(
        c_shift_clock_synchronizer : boolean := TRUE; -- try to get out_clock in sync with clk_pixel
        c_parallel                 : boolean := TRUE; -- default output parallel data
        c_serial                   : boolean := TRUE; -- default output serial data
        c_ddr                      : boolean := FALSE; -- default use SDR for serial data
        c_depth                    : integer := 8
    );
    Port(
        clk_pixel                       : in  std_logic; -- VGA pixel clock, 25 MHz for 640x480
        clk_shift                       : in  std_logic; -- SDR: 10x clk_pixel, DDR: 5x clk_pixel, in phase with clk_pixel
        in_red                          : in  std_logic_vector(c_depth - 1 downto 0);
        in_green                        : in  std_logic_vector(c_depth - 1 downto 0);
        in_blue                         : in  std_logic_vector(c_depth - 1 downto 0);
        in_blank                        : in  std_logic;
        in_hsync                        : in  std_logic;
        in_vsync                        : in  std_logic;
        -- parallel outputs
        outp_red, outp_green, outp_blue : out std_logic_vector(9 downto 0);
        -- serial outputs
        out_red                         : out std_logic_vector(1 downto 0);
        out_green                       : out std_logic_vector(1 downto 0);
        out_blue                        : out std_logic_vector(1 downto 0);
        out_clock                       : out std_logic_vector(1 downto 0)
    );
end vga2dvid;

architecture Behavioral of vga2dvid is
    signal encoded_red, encoded_green, encoded_blue : std_logic_vector(9 downto 0);
    signal latched_red, latched_green, latched_blue : std_logic_vector(9 downto 0) := (others => '0');
    signal shift_red, shift_green, shift_blue       : std_logic_vector(9 downto 0) := (others => '0');
    constant c_shift_clock_initial                  : std_logic_vector(9 downto 0) := "0000011111";
    signal shift_clock                              : std_logic_vector(9 downto 0) := c_shift_clock_initial;
    signal R_shift_clock_off_sync                   : std_logic                    := '0';
    signal R_shift_clock_synchronizer               : unsigned(7 downto 0)         := (others => '0');
    signal R_sync_fail                              : unsigned(6 downto 0); -- counts sync fails, after too many, reinitialize shift_clock

    constant c_red   : std_logic_vector(1 downto 0) := (others => '0');
    constant c_green : std_logic_vector(1 downto 0) := (others => '0');
    signal c_blue    : std_logic_vector(1 downto 0);

    signal red_d   : std_logic_vector(7 downto 0);
    signal green_d : std_logic_vector(7 downto 0);
    signal blue_d  : std_logic_vector(7 downto 0);
begin
    c_blue <= in_vsync & in_hsync;

    red_d(7 downto 8 - c_depth)   <= in_red(c_depth - 1 downto 0);
    green_d(7 downto 8 - c_depth) <= in_green(c_depth - 1 downto 0);
    blue_d(7 downto 8 - c_depth)  <= in_blue(c_depth - 1 downto 0);

    -- fill vacant low bits with value repeated (so min/max value is always 0 or 255)
    G_vacant_bits : if c_depth < 8 generate
        G_bits : for i in 0 to 8 - c_depth - 1 generate
            red_d(i)   <= in_red(0);
            green_d(i) <= in_green(0);
            blue_d(i)  <= in_blue(0);
        end generate;
    end generate;

    G_shift_clock_synchronizer : if c_shift_clock_synchronizer generate
        -- sampler verifies is shift_clock state synchronous with pixel_clock
        process(clk_pixel)
        begin
            if rising_edge(clk_pixel) then
                -- does 0 to 1 transition at bits 5 downto 4 happen at rising_edge of clk_pixel?
                -- if shift_clock = c_shift_clock_initial then
                if shift_clock(5 downto 4) = c_shift_clock_initial(5 downto 4) then -- same as above line but simplified
                    R_shift_clock_off_sync <= '0';
                else
                    R_shift_clock_off_sync <= '1';
                end if;
            end if;
        end process;
        -- every N cycles of clk_shift: signal to skip 1 cycle in order to get in sync
        process(clk_shift)
        begin
            if rising_edge(clk_shift) then
                if R_shift_clock_off_sync = '1' then
                    if R_shift_clock_synchronizer(R_shift_clock_synchronizer'high) = '1' then
                        R_shift_clock_synchronizer <= (others => '0');
                    else
                        R_shift_clock_synchronizer <= R_shift_clock_synchronizer + 1;
                    end if;
                else
                    R_shift_clock_synchronizer <= (others => '0');
                end if;
            end if;
        end process;
    end generate;                       -- shift_clock_synchronizer

    u21 : entity work.tmds_encoder
        PORT MAP(clk => clk_pixel, data => red_d, c => c_red, blank => in_blank, encoded => encoded_red);
    u22 : entity work.tmds_encoder
        PORT MAP(clk => clk_pixel, data => green_d, c => c_green, blank => in_blank, encoded => encoded_green);
    u23 : entity work.tmds_encoder
        PORT MAP(clk => clk_pixel, data => blue_d, c => c_blue, blank => in_blank, encoded => encoded_blue);

    process(clk_pixel)
    begin
        if rising_edge(clk_pixel) then
            latched_red   <= encoded_red;
            latched_green <= encoded_green;
            latched_blue  <= encoded_blue;
        end if;
    end process;

    G_parallel : if c_parallel generate
        outp_red   <= latched_red;
        outp_green <= latched_green;
        outp_blue  <= latched_blue;
    end generate;

    G_SDR : if c_serial and not c_ddr generate
        process(clk_shift)
        begin
            if rising_edge(clk_shift) then
                --if shift_clock = "0000011111" then
                if shift_clock(5 downto 4) = c_shift_clock_initial(5 downto 4) then -- same as above line but simplified
                    shift_red   <= latched_red;
                    shift_green <= latched_green;
                    shift_blue  <= latched_blue;
                else
                    shift_red   <= "0" & shift_red(9 downto 1);
                    shift_green <= "0" & shift_green(9 downto 1);
                    shift_blue  <= "0" & shift_blue(9 downto 1);
                end if;
                if R_shift_clock_synchronizer(R_shift_clock_synchronizer'high) = '0' then
                    shift_clock <= shift_clock(0) & shift_clock(9 downto 1);
                else
                    -- synchronization failed.
                    -- after too many fails, reinitialize shift_clock
                    if R_sync_fail(R_sync_fail'high) = '1' then
                        shift_clock <= c_shift_clock_initial;
                        R_sync_fail <= (others => '0');
                    else
                        R_sync_fail <= R_sync_fail + 1;
                    end if;
                end if;
            end if;
        end process;
    end generate;

    G_DDR : if c_serial and c_ddr generate
        process(clk_shift)
        begin
            if rising_edge(clk_shift) then
                --if shift_clock = "0000011111" then
                if shift_clock(5 downto 4) = c_shift_clock_initial(5 downto 4) then -- same as above line but simplified
                    shift_red   <= latched_red;
                    shift_green <= latched_green;
                    shift_blue  <= latched_blue;
                else
                    shift_red   <= "00" & shift_red(9 downto 2);
                    shift_green <= "00" & shift_green(9 downto 2);
                    shift_blue  <= "00" & shift_blue(9 downto 2);
                end if;
                if R_shift_clock_synchronizer(R_shift_clock_synchronizer'high) = '0' then
                    shift_clock <= shift_clock(1 downto 0) & shift_clock(9 downto 2);
                else
                    -- synchronization failed.
                    -- after too many fails, reinitialize shift_clock
                    if R_sync_fail(R_sync_fail'high) = '1' then
                        shift_clock <= c_shift_clock_initial;
                        R_sync_fail <= (others => '0');
                    else
                        R_sync_fail <= R_sync_fail + 1;
                    end if;
                end if;
            end if;
        end process;
    end generate;

    -- SDR: use only bit 0 from each out_* channel 
    -- DDR: 2 bits per 1 clock period,
    -- (one bit output on rising edge, other on falling edge of clk_shift)
    G_serial : if c_serial generate
        out_red   <= shift_red(1 downto 0);
        out_green <= shift_green(1 downto 0);
        out_blue  <= shift_blue(1 downto 0);
        out_clock <= shift_clock(1 downto 0);
    end generate;

end Behavioral;
