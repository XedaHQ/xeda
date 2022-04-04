library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pipelined_adder_tb is
  generic(
    G_CLOCK_PERIOD_PS   : positive := 15_000;
    G_WAIT_PRE_RESET_NS : positive := 100
  );
end pipelined_adder_tb;

architecture TB of pipelined_adder_tb is

  constant W : positive := 32;
  component pipelined_adder is
    -- post-synthesis simulation wont work as netlist has no generics
    -- generic(
    --   W : positive := 32
    -- );
    port(
      clock     : in  std_logic;
      reset     : in  std_logic;
      in_a      : in  std_logic_vector(W - 1 downto 0);
      in_b      : in  std_logic_vector(W - 1 downto 0);
      in_valid  : in  std_logic;
      in_ready  : out std_logic;
      out_sum   : out std_logic_vector(W downto 0);
      out_valid : out std_logic;
      out_ready : in  std_logic
    );
  end component;
  constant clock_period : time := G_CLOCK_PERIOD_PS * ps;

  signal in_ready, out_valid               : std_logic;
  signal clock, reset, in_valid, out_ready : std_logic := '0';
  signal stop_clock, reset_done            : boolean   := FALSE;
  signal a, b                              : std_logic_vector(W - 1 downto 0);
  signal s                                 : std_logic_vector(W downto 0);

  type test_vector is record
    a, b : std_logic_vector(W - 1 downto 0);
    s    : std_logic_vector(W downto 0);
  end record;

  type test_vector_array is array (natural range <>) of test_vector;
  constant test_vectors : test_vector_array := (
    -- a, b, s
    (32X"111", 32X"022", 33X"133"),
    (32X"244", 32X"122", 33X"366"),
    (32X"311", 32X"0ab", 33X"3bc"),
    (32X"222", 32X"555", 33X"777")
  );
begin
  uut : pipelined_adder
      -- generic map(
      --   W => W
      -- )
    port map(
      clock     => clock,
      reset     => reset,
      in_a      => a,
      in_b      => b,
      in_valid  => in_valid,
      in_ready  => in_ready,
      out_sum   => s,
      out_valid => out_valid,
      out_ready => out_ready
    );
  -- generate clock
  CLOCK_PROCESS : process
  begin
    clock <= '0';
    if not stop_clock then
      clock <= not clock;
      wait for clock_period / 2;
    else
      wait;
    end if;
  end process;

  RESET_PROCESS : process
  begin
    reset      <= '0';
    wait for G_WAIT_PRE_RESET_NS * ns;
    wait until falling_edge(clock);
    reset      <= '1';
    wait for clock_period;
    reset      <= not reset;
    wait until rising_edge(clock);
    report "RESET DONE" severity note;
    reset_done <= True;
    wait;
  end process;

  STIMULUS_PROCESS : process
  begin
    in_valid <= '0';
    wait until reset_done;
    for i in test_vectors'range loop
      a        <= test_vectors(i).a;
      b        <= test_vectors(i).b;
      in_valid <= '1';
      loop
        wait until rising_edge(clock);
        if in_ready then
          exit;
        end if;
      end loop;
      in_valid <= '0';
    end loop;
    report "End of Inputs";
    wait;
  end process;

  MONITOR_PROCESS : process
  begin
    out_ready  <= '0';
    wait until reset_done;
    for i in test_vectors'range loop
      out_ready <= '1';
      loop
        wait until rising_edge(clock);
        if out_valid then
          exit;
        end if;
      end loop;
      report "test_vector " & integer'image(i) & " a=" & to_hstring(test_vectors(i).a) & " b=" & to_hstring(test_vectors(i).b) & " received: " & to_hstring(s) severity NOTE;
      assert s = test_vectors(i).s report "test_vector " & integer'image(i) & " failed. Output: " & to_hstring(s) & " Expected:" & to_hstring(test_vectors(i).s) severity failure;
      out_ready <= '0';
    end loop;
    report "full_adder_tb finished!";
    stop_clock <= TRUE;
    wait;
  end process;
end;
