-------------------------------------------------------------------------------
-- Testbench for full_adder
-- Adopted from https://vhdlguide.readthedocs.io/en/latest/vhdl/testbench.html
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;

entity full_adder_tb is
end full_adder_tb;

architecture behavior of full_adder_tb is
  component full_adder is
    port (
      a  : in std_logic;
      b  : in std_logic;
      ci : in std_logic;
      s  : out std_logic;
      co : out std_logic);
  end component;
  signal a, b, ci, s, co : std_logic;

  type test_vector is record
    a, b, ci : std_logic;
    s, co    : std_logic;
  end record;

  type test_vector_array is array (natural range <>) of test_vector;
  constant test_vectors : test_vector_array := (
    -- a, b, ci, s , co
    ('0', '0', '0', '0', '0'),
    ('0', '0', '1', '1', '0'),
    ('0', '1', '0', '1', '0'),
    ('1', '0', '0', '1', '0'),
    ('1', '1', '0', '0', '1'),
    ('0', '1', '1', '0', '1'),
    ('1', '0', '1', '0', '1'),
    ('1', '1', '1', '1', '1')
  );
begin
  uut : full_adder port map(
    a  => a,
    b  => b,
    ci => ci,
    s  => s,
    co => co
  );
  stim_proc : process
    constant delay : time := 10 ns;
  begin
    for i in test_vectors'range loop
      a  <= test_vectors(i).a;
      b  <= test_vectors(i).b;
      ci <= test_vectors(i).ci;
      wait for delay;
      assert (s = test_vectors(i).s) and (co = test_vectors(i).co)
      report "test_vector " & integer'image(i) & " failed" severity failure;
    end loop;
    report "full_adder_tb finished!";
    wait;
  end process;
end;