library ieee;
use ieee.std_logic_1164.all;

entity full_adder_piped is
  port (
    clk   : in std_logic;
    reset : in std_logic;
    a     : in std_logic;
    b     : in std_logic;
    ci    : in std_logic;
    s     : out std_logic;
    co    : out std_logic);
end;

architecture behavioral of full_adder_piped is
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if reset = '1' then
        s  <= '0';
        co <= '0';
      else
        s  <= a xor b xor ci;
        co <= (a and b) or (b and ci) or (ci and a);
      end if; -- reset
    end if; -- clk rising_edge
  end process;
end;