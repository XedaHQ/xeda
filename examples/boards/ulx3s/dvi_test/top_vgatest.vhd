-- (c)EMARD
-- License=BSD
-- edited by Kamyar

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.numeric_std_unsigned.all;

entity top_vgatest is
  generic(
    --  640x400  @50Hz
    --  640x400  @60Hz
    --  640x480  @50Hz
    --  640x480  @60Hz
    --  720x576  @50Hz
    --  720x576  @60Hz
    --  800x480  @60Hz
    --  800x600  @60Hz
    -- 1024x768  @60Hz
    -- 1280x768  @60Hz
    -- 1366x768  @60Hz
    -- 1280x1024 @60Hz
    -- 1920x1080 @30Hz
    -- 1920x1080 @50Hz overclock 540MHz
    -- 1920x1200 @30Hz overclock 375MHz
    -- 1920x1200 @50Hz overclock 600MHz
    G_X        : positive := 1920;      -- pixels
    G_Y        : positive := 1200;      -- pixels
    G_FREQ     : positive := 30;        -- Hz 60,50,30
    G_X_ADJUST : integer  := 0;         -- adjust -3..3 if no picture or to fine-tune G_FREQ
    G_Y_ADJUST : integer  := 0;         -- adjust -3..3 if no picture or to fine-tune G_FREQ
    G_EXT_GPDI : boolean  := FALSE;     -- FALSE:disable, TRUE:enable external gpdi
    G_DDR      : boolean  := TRUE;      -- FALSE:SDR, TRUE:DDR
    G_C_DEPTH  : positive := 8          -- color depth (bits)
  );
  port(
    -- main clock input from 25MHz clock source
    clk_25mhz : in    std_logic;
    -- LEDs (active high)
    led       : out   std_logic_vector(7 downto 0);
    -- buttons (active high)
    btn       : in    std_logic_vector(6 downto 0);
    -- GPIO (some are shared with wifi and adc)
    gp        : inout std_logic_vector(27 downto 0) := (others => 'Z');
    gn        : inout std_logic_vector(27 downto 0) := (others => 'Z');
    -- Digital Video (differential outputs)
    gpdi_dp   : out   std_logic_vector(3 downto 0)
  );
end;

architecture Behavioral of top_vgatest is
  type T_NATURAL_ARRAY is array (natural range <>) of natural;
  constant c_possible_freqs : T_NATURAL_ARRAY := (
    25000000,
    27000000,
    40000000,
    50000000,
    54000000,
    60000000,
    65000000,
    75000000,
    80000000,                           -- overclock 400MHz
    100000000,                          -- overclock 500MHz
    108000000,                          -- overclock 540MHz
    120000000                           -- overclock 600MHz
  );

  function F_find_next_f(f : natural) return natural is
    variable f0 : natural := 0;
  begin
    for fx in c_possible_freqs'range loop
      f0 := c_possible_freqs(fx);
      if f0 >= f then
        return f0;
      end if;
    end loop;
    assert FALSE report "cant find next f for " & integer'image(f) severity failure;
  end F_find_next_f;

  --! Returns the number of bits required to represet values less than n (0 to n - 1 inclusive)
  function log2ceil(n : natural) return natural is
    variable r : natural := 0;
  begin
    while n > 2 ** r loop
      r := r + 1;
    end loop;
    return r;
  end function;

  constant xminblank   : natural := G_X / 64; -- initial estimate
  constant yminblank   : natural := G_Y / 64; -- for minimal blank space
  constant min_pixel_f : natural := G_FREQ * (G_X + xminblank) * (G_Y + yminblank);
  constant pixel_f     : natural := F_find_next_f(min_pixel_f);
  constant yframe      : natural := G_Y + yminblank;
  constant xframe      : natural := pixel_f / (G_FREQ * yframe);
  constant xblank      : integer := xframe - G_X;
  constant yblank      : integer := yframe - G_Y;

  signal clk_pixel, clk_shift                        : std_logic;
  signal vga_hsync, vga_vsync, vga_blank             : std_logic;
  signal vga_r, vga_g, vga_b                         : std_logic_vector(G_C_DEPTH - 1 downto 0);
  signal dvid_red, dvid_green, dvid_blue, dvid_clock : std_logic_vector(1 downto 0);

  component ODDRX1F
    generic(
      GSR : string := "ENABLED"
    );
    port(
      SCLK : in  std_logic;
      RST  : in  std_logic;
      D0   : in  std_logic;
      D1   : in  std_logic;
      Q    : out std_logic
    );
  end component;

begin
  assert false report "pixel_f=" & integer'image(pixel_f) severity note;

  clk_single_pll : entity work.ecp5pll
    generic map(
      G_IN_HZ        => 25_000_000,
      G_OUT_0_HZ     => pixel_f * 5,
      G_OUT_0_TOL_HZ => 1_666_666,
      G_OUT_1_HZ     => pixel_f,
      G_OUT_1_TOL_HZ => 333333
    )
    port map(
      clk_i   => clk_25mhz,
      clk_o_0 => clk_shift,
      clk_o_1 => clk_pixel
    );

  vga_instance : entity work.vga
    generic map(
      G_X_RES             => G_X,
      c_hsync_front_porch => xblank / 3,
      c_hsync_pulse       => xblank / 3,
      c_hsync_back_porch  => xblank - 2 * (xblank / 3) + G_X_ADJUST,
      G_Y_RES             => G_Y,
      c_vsync_front_porch => yblank / 3,
      c_vsync_pulse       => yblank / 3,
      c_vsync_back_porch  => yblank - 2 * (yblank / 3) + G_Y_ADJUST,
      G_X_BITS            => log2ceil(G_X + G_X_ADJUST + xblank), --12,
      G_Y_BITS            => log2ceil(G_Y + G_Y_ADJUST + yblank),
      G_DEPTH             => G_C_DEPTH
    )
    port map(
      clk_pixel     => clk_pixel,
      clk_pixel_ena => '1',
      vga_r         => vga_r,
      vga_g         => vga_g,
      vga_b         => vga_b,
      vga_hsync     => vga_hsync,
      vga_vsync     => vga_vsync,
      vga_blank     => vga_blank
    );

  led <= (
    0      => vga_hsync,
    1      => vga_vsync,
    7      => vga_blank,
    others => '0'
  );

  vga2dvid_instance : entity work.vga2dvid
    generic map(
      c_ddr                      => G_DDR,
      c_shift_clock_synchronizer => FALSE,
      c_depth                    => G_C_DEPTH
    )
    port map(
      clk_pixel => clk_pixel,
      clk_shift => clk_shift,
      in_red    => vga_r,
      in_green  => vga_g,
      in_blue   => vga_b,
      in_hsync  => vga_hsync,
      in_vsync  => vga_vsync,
      in_blank  => vga_blank,
      -- single-ended output ready for differential buffers
      out_red   => dvid_red,
      out_green => dvid_green,
      out_blue  => dvid_blue,
      out_clock => dvid_clock
    );

  -- vendor specific DDR modules
  -- convert SDR 2-bit input to DDR clocked 1-bit output (single-ended)
  ddr_clock : ODDRX1F port map(D0 => dvid_clock(0), D1 => dvid_clock(1), Q => gpdi_dp(3), SCLK => clk_shift, RST => '0');
  ddr_red : ODDRX1F port map(D0 => dvid_red(0), D1 => dvid_red(1), Q => gpdi_dp(2), SCLK => clk_shift, RST => '0');
  ddr_green : ODDRX1F port map(D0 => dvid_green(0), D1 => dvid_green(1), Q => gpdi_dp(1), SCLK => clk_shift, RST => '0');
  ddr_blue : ODDRX1F port map(D0 => dvid_blue(0), D1 => dvid_blue(1), Q => gpdi_dp(0), SCLK => clk_shift, RST => '0');

  g_external_gpdi : if G_EXT_GPDI generate
    ddr_xclock : ODDRX1F port map(D0 => dvid_clock(0), D1 => dvid_clock(1), Q => gp(12), SCLK => clk_shift, RST => '0');
    ddr_xred : ODDRX1F port map(D0 => dvid_red(0), D1 => dvid_red(1), Q => gp(11), SCLK => clk_shift, RST => '0');
    ddr_xgreen : ODDRX1F port map(D0 => dvid_green(0), D1 => dvid_green(1), Q => gp(10), SCLK => clk_shift, RST => '0');
    ddr_xblue : ODDRX1F port map(D0 => dvid_blue(0), D1 => dvid_blue(1), Q => gp(9), SCLK => clk_shift, RST => '0');
  end generate;

end Behavioral;
