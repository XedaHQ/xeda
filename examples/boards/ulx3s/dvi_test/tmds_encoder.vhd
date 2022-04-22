----------------------------------------------------------------------------------
-- Description: TMDS Encoder 
--     8 bits colour, 2 control bits and one blanking bits in
--       10 bits of TMDS encoded data out
--     Clocked at the pixel clock
--
----------------------------------------------------------------------------------
-- Copyright (c) 2012 Mike Field <hamster@snap.net.nz>
-- Copyright (c) 2022 Kamyar Mohajerani <kammoh@gmail.com>
-- 
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

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.numeric_std_unsigned.all;

use work.utils_pkg.all;

entity tmds_encoder is
   Port(
      clk     : in  std_logic;
      data    : in  std_logic_vector(7 downto 0);
      c       : in  std_logic_vector(1 downto 0);
      blank   : in  std_logic;
      encoded : out std_logic_vector(9 downto 0)
   );
end tmds_encoder;

architecture Behavioral of tmds_encoder is
   -- Registers
   signal encoded_reg : std_logic_vector(encoded'range);
   signal dc_bias     : unsigned(3 downto 0) := (others => '0');

   -- Wires
   signal xored, xnored       : std_logic_vector(8 downto 0);
   signal num_ones            : unsigned(3 downto 0);
   signal data_word           : std_logic_vector(8 downto 0);
   signal data_word_inv       : std_logic_vector(8 downto 0);
   signal data_word_disparity : unsigned(3 downto 0);
begin
   encoded <= encoded_reg;

   -- Work our the two different encodings for the byte
   xored(0)  <= data(0);
   xored(8)  <= '1';
   xnored(0) <= data(0);
   xnored(8) <= '0';

   GEN_BYTE_ENCODE : for i in 1 to 7 generate
      xored(i)  <= data(i) xor xored(i - 1);
      xnored(i) <= data(i) xnor xnored(i - 1);
   end generate GEN_BYTE_ENCODE;

   -- Count how many ones are set in data
   num_ones <= sum_bits(data);

   -- Decide which encoding to use
   data_word     <= xnored when num_ones > 4 or (num_ones = 4 and data(0) = '0') else
                    xored;
   data_word_inv <= not data_word;

   -- Work out the DC bias of the dataword;
   data_word_disparity <= "1100" + sum_bits(data_word(7 downto 0));

   -- Now work out what the output should be
   process(clk)
   begin
      if rising_edge(clk) then
         if blank = '1' then
            -- In the control periods, all values have and have balanced bit count
            case c is
               when "00"   => encoded_reg <= "1101010100";
               when "01"   => encoded_reg <= "0010101011";
               when "10"   => encoded_reg <= "0101010100";
               when others => encoded_reg <= "1010101011";
            end case;
            dc_bias <= (others => '0');
         else
            if dc_bias = "00000" or data_word_disparity = 0 then
               -- dataword has no disparity
               if data_word(8) = '1' then
                  encoded_reg <= "01" & data_word(7 downto 0);
                  dc_bias     <= dc_bias + data_word_disparity;
               else
                  encoded_reg <= "10" & data_word_inv(7 downto 0);
                  dc_bias     <= dc_bias - data_word_disparity;
               end if;
            elsif (dc_bias(3) = '0' and data_word_disparity(3) = '0') or (dc_bias(3) = '1' and data_word_disparity(3) = '1') then
               encoded_reg <= '1' & data_word(8) & data_word_inv(7 downto 0);
               dc_bias     <= dc_bias + data_word(8) - data_word_disparity;
            else
               encoded_reg <= '0' & data_word;
               dc_bias     <= dc_bias - data_word_inv(8) + data_word_disparity;
            end if;
         end if;
      end if;
   end process;
end Behavioral;
