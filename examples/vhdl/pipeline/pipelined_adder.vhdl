library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pipelined_adder is
   generic(
      W : positive := 32
   );
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
end;

architecture behavioral of pipelined_adder is
   constant DEPTH : positive := 2;

   -- registers
   signal l0_a, l0_b : std_logic_vector(W - 1 downto 0);
   signal l1_s       : std_logic_vector(W downto 0);
   signal valid_pipe : std_logic_vector(0 to DEPTH - 1);

   -- wires
   signal valids  : std_logic_vector(0 to DEPTH);
   signal readies : std_logic_vector(0 to DEPTH);
begin

   process(all)
   begin
      readies(DEPTH) <= out_ready;
      in_ready       <= readies(0);
      valids(0)      <= in_valid;
      out_valid      <= valids(DEPTH);

      for i in 0 to DEPTH - 1 loop
         readies(i)    <= not valid_pipe(i) or readies(i + 1);
         valids(i + 1) <= valid_pipe(i);
      end loop;

      out_sum <= l1_s;
   end process;

   process(clock)
   begin
      if rising_edge(clock) then
         if reset = '1' then
            valid_pipe <= (others => '0');
         else
            for i in 0 to DEPTH - 1 loop
               if readies(i) then
                  valid_pipe(i) <= valids(i);
               end if;
            end loop;
         end if;
      end if;
   end process;

   process(clock)
   begin
      if rising_edge(clock) then
         -- Update logic for each stage: 
         if readies(0) then
            l0_a <= in_a;
            l0_b <= in_b;
         end if;
         if readies(1) then
            l1_s <= std_logic_vector(unsigned('0' & l0_a) + unsigned(l0_b));
         end if;
      end if;
   end process;
end;
