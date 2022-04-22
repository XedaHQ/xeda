-- AUTHOR=EMARD
-- LICENSE=BSD
--
-- Generates VGA picture from sequential bitmap data from pixel clock synchronous FIFO.
-- signal 'fetch_next' is set high for 1 clk_pixel period as soon as current pixel data is consumed
-- fifo should be fast enough to fetch new data for new pixel
--
-- modified by Kamyar

LIBRARY ieee;
USE ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity vga is
  generic(
    G_X_RES             : integer  := 640;
    c_hsync_front_porch : integer  := 16;
    c_hsync_pulse       : integer  := 96;
    c_hsync_back_porch  : integer  := 44; -- 48
    G_Y_RES             : integer  := 480;
    c_vsync_front_porch : integer  := 10;
    c_vsync_pulse       : integer  := 2;
    c_vsync_back_porch  : integer  := 31; -- 33
    G_X_BITS            : integer  := 10; -- should fit c_resolution_x + c_hsync_front_porch + c_hsync_pulse + c_hsync_back_porch
    G_Y_BITS            : integer  := 10; -- should fit c_resolution_y + c_vsync_front_porch + c_vsync_pulse + c_vsync_back_porch
    G_DEPTH             : positive := 8
    -- c_dbl_x             : integer :=   0; -- 0-normal X, 1-double X
    -- c_dbl_y             : integer :=   0  -- 0-normal X, 1-double X
  );
  port(
    clk_pixel                     : in  std_logic; -- pixel clock, 25 MHz for 640x480
    clk_pixel_ena                 : in  std_logic := '1'; -- pixel clock ena
    -- test_picture                  : in  std_logic                    := '0'; -- '1' to show test picture
    fetch_next                    : out std_logic; -- request FIFO to fetch next pixel data
    beam_x                        : out std_logic_vector(G_X_BITS - 1 downto 0);
    beam_y                        : out std_logic_vector(G_Y_BITS - 1 downto 0);
    -- r_i, g_i, b_i                 : in  std_logic_vector(c_depth - 1 downto 0) := (others => '0'); -- pixel data from FIFO
    vga_r, vga_g, vga_b           : out std_logic_vector(G_DEPTH - 1 downto 0); -- 8-bit VGA video signal out
    vga_hsync, vga_vsync          : out std_logic; -- VGA sync
    vga_vblank, vga_blank, vga_de : out std_logic -- V blank for CPU interrupts and H+V blank for digital encoder (HDMI)
  );
end vga;

architecture RTL of vga is
  signal x_counter : unsigned(G_X_BITS - 1 downto 0); -- (9 downto 0) is good for up to 1023 frame timing width (resolution 640x480)
  signal y_counter : unsigned(G_Y_BITS - 1 downto 0); -- (9 downto 0) is good for up to 1023 frame timing width (resolution 640x480)

  constant c_hblank_on                     : unsigned(x_counter'range) := to_unsigned(G_X_RES - 1, G_X_BITS);
  constant c_hsync_on                      : unsigned(x_counter'range) := to_unsigned(G_X_RES + c_hsync_front_porch - 1, G_X_BITS);
  constant c_hsync_off                     : unsigned(x_counter'range) := to_unsigned(G_X_RES + c_hsync_front_porch + c_hsync_pulse - 1, G_X_BITS);
  constant c_hblank_off                    : unsigned(x_counter'range) := to_unsigned(G_X_RES + c_hsync_front_porch + c_hsync_pulse + c_hsync_back_porch - 1, G_X_BITS);
  constant c_frame_x                       : unsigned(x_counter'range) := to_unsigned(G_X_RES + c_hsync_front_porch + c_hsync_pulse + c_hsync_back_porch - 1, G_X_BITS);
  -- frame_x = 640 + 16 + 96 + 48 = 800;
  constant c_vblank_on                     : unsigned(y_counter'range) := to_unsigned(G_Y_RES - 1, G_Y_BITS);
  constant c_vsync_on                      : unsigned(y_counter'range) := to_unsigned(G_Y_RES + c_vsync_front_porch - 1, G_Y_BITS);
  constant c_vsync_off                     : unsigned(y_counter'range) := to_unsigned(G_Y_RES + c_vsync_front_porch + c_vsync_pulse - 1, G_Y_BITS);
  constant c_vblank_off                    : unsigned(y_counter'range) := to_unsigned(G_Y_RES + c_vsync_front_porch + c_vsync_pulse + c_vsync_back_porch - 1, G_Y_BITS);
  constant c_frame_y                       : unsigned(y_counter'range) := to_unsigned(G_Y_RES + c_vsync_front_porch + c_vsync_pulse + c_vsync_back_porch - 1, G_Y_BITS);
  -- frame_y = 480 + 10 + 2 + 33 = 525;
  -- refresh_rate = pixel_clock/(frame_x*frame_y) = 25MHz / (800*525) = 59.52Hz
  signal R_hsync, R_vsync, R_blank, R_disp : std_logic; -- disp = not blank
  signal R_disp_early, R_vdisp             : std_logic; -- blank generation
  signal R_blank_early, R_vblank           : std_logic; -- blank generation
  signal R_fetch_next                      : std_logic;
  signal R_vga_r, R_vga_g, R_vga_b         : std_logic_vector(G_DEPTH - 1 downto 0);
  -- test picture generation
  signal W, A, T                           : std_logic_vector(G_DEPTH - 1 downto 0);
  signal Z                                 : std_logic_vector(G_DEPTH - 3 downto 0);

begin

  assert FALSE report "VGA parameters:" & --
  LF & "    G_X_RES  : " & integer'image(G_X_RES) & --
  LF & "    G_Y_RES  : " & integer'image(G_Y_RES) & --
  LF & "    G_X_BITS : " & integer'image(G_X_BITS) & --
  LF & "    G_Y_BITS : " & integer'image(G_Y_BITS) & --
  LF & "    G_DEPTH  : " & integer'image(G_DEPTH) & --
  LF severity NOTE;

  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if clk_pixel_ena = '1' then
        if x_counter = c_frame_x then
          x_counter <= (others => '0');
          if y_counter = c_frame_y then
            y_counter <= (others => '0');
          else
            y_counter <= y_counter + 1;
          end if;
        else
          x_counter <= x_counter + 1;
        end if;
        R_fetch_next <= R_disp_early;
      else
        R_fetch_next <= '0';
      end if;
    end if;
  end process;

  beam_x <= std_logic_vector(x_counter);
  beam_y <= std_logic_vector(y_counter);

  fetch_next <= R_fetch_next;

  -- generate sync and blank
  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if x_counter = c_hblank_on then
        R_blank_early <= '1';
        R_disp_early  <= '0';
      elsif x_counter = c_hblank_off then
        R_blank_early <= R_vblank;      -- "OR" function
        R_disp_early  <= R_vdisp;       -- "AND" function
      end if;
    end if;
  end process;
  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if x_counter = c_hsync_on then
        R_hsync <= '1';
      elsif x_counter = c_hsync_off then
        R_hsync <= '0';
      end if;
    end if;
  end process;
  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if y_counter = c_vblank_on then
        R_vblank <= '1';
        R_vdisp  <= '0';
      elsif y_counter = c_vblank_off then
        R_vblank <= '0';
        R_vdisp  <= '1';
      end if;
    end if;
  end process;
  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if y_counter = c_vsync_on then
        R_vsync <= '1';
      elsif y_counter = c_vsync_off then
        R_vsync <= '0';
      end if;
    end if;
  end process;

  -- test picture generator
  A <= (others => '1') when std_logic_vector(x_counter(7 downto 5)) = "010" and std_logic_vector(y_counter(7 downto 5)) = "010" else (others => '0');
  W <= (others => '1') when x_counter(7 downto 0) = y_counter(7 downto 0) else (others => '0');
  Z <= (others => '1') when std_logic_vector(y_counter(4 downto 3)) = not std_logic_vector(x_counter(4 downto 3)) else (others => '0');
  T <= (others => y_counter(6));
  process(clk_pixel)
  begin
    if rising_edge(clk_pixel) then
      if R_blank = '1' then
        -- analog VGA needs this, DVI doesn't
        R_vga_r <= (others => '0');
        R_vga_g <= (others => '0');
        R_vga_b <= (others => '0');
      else
        R_vga_r <= (((std_logic_vector(x_counter(G_DEPTH - 3 downto 0)) and Z) & "00") or W) and not A;
        R_vga_g <= ((std_logic_vector(x_counter(G_DEPTH - 1 downto 0)) and T) or W) and not A;
        R_vga_b <= std_logic_vector(y_counter(G_DEPTH - 1 downto 0)) or W or A;
      end if;
      R_blank <= R_blank_early;
      R_disp  <= R_disp_early;
    end if;
  end process;

  vga_r      <= R_vga_r;
  vga_g      <= R_vga_g;
  vga_b      <= R_vga_b;
  vga_hsync  <= R_hsync;
  vga_vsync  <= R_vsync;
  vga_blank  <= R_blank;
  vga_vblank <= R_vblank;
  vga_de     <= R_disp;

end RTL;
