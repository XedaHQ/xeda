--===============================================================================================--
--! @brief         Compute square-root of an integer
--! @author        Kamyar Mohajerani (kamyar@ieee.org)
--! @copyright     Copyright (c) 2022
--! @license       Solderpad Hardware License v2.1 (SHL-2.1)
--! @brief         Compute square-root of an integer
--! @vhdl          VHDL 2008
--!
--! @description   
--!                Based on the SystemVerilog implementation by Will Green 
--!                  at https://github.com/projf/projf-explore/blob/master/lib/maths/sqrt_int.sv
--!
--! @parameters    G_IN_WIDTH: width of input
--!
--===============================================================================================--

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity sqrt is
    generic(
        G_IN_WIDTH : positive           --@ Input width
    );

    port(
        clk            : in  std_logic;
        rst            : in  std_logic;
        radicand       : in  std_logic_vector(G_IN_WIDTH - 1 downto 0);
        radicand_valid : in  std_logic;
        radicand_ready : out std_logic;
        root           : out std_logic_vector((G_IN_WIDTH + 1) / 2 - 1 downto 0);
        root_remainder : out std_logic_vector((G_IN_WIDTH + 1) / 2 downto 0);
        root_valid     : out std_logic;
        root_ready     : in  std_logic
    );
end entity sqrt;

architecture RTL of sqrt is

    --======================================== Constants ========================================--
    constant ITER : natural := (G_IN_WIDTH + 1) / 2;
    constant W    : natural := ITER * 2;
    --========================================== Types ==========================================--
    type T_STATE is (S_IDLE, S_BUSY, S_DONE);
    --======================================== Functions ========================================--
    function clog2(n : positive) return natural is
        variable r    : natural  := 0;
        variable pow2 : positive := 1;
    begin
        while n > pow2 loop
            pow2 := pow2 * 2;
            r    := r + 1;
        end loop;
        return r;
    end function;

    --======================================== Registers ========================================--
    signal state        : T_STATE;
    signal counter      : unsigned(clog2(ITER) - 1 downto 0);
    signal acc          : unsigned(W + 1 downto 0);
    signal q            : unsigned(W - 1 downto 0);
    signal x            : unsigned(W - 3 downto 0);
    --========================================== Wires ==========================================--
    signal test_res     : unsigned(W + 1 downto 0);
    signal test_res_msb : std_logic;
begin
    test_res       <= acc - (q & "01");
    test_res_msb   <= test_res(test_res'length - 1);
    root           <= std_logic_vector(resize(q, root'length));
    root_remainder <= std_logic_vector(acc(root_remainder'length + 1 downto 2));

    process(all)
    begin
        radicand_ready <= '0';
        root_valid     <= '0';
        case state is
            when S_IDLE =>
                radicand_ready <= '1';
            when S_BUSY =>
            when S_DONE =>
                root_valid <= '1';
        end case;
    end process;

    process(clk)
    begin
        if rising_edge(clk) then
            if rst then
                state <= S_IDLE;
            else
                case state is
                    when S_IDLE =>
                        if radicand_valid = '1' then
                            counter <= (others => '0');
                            q       <= (others => '0');
                            -- (acc, x) <= resize(unsigned(radicand), 2 * W);
                            x       <= unsigned(radicand(W - 3 downto 0));
                            acc     <= resize(unsigned(radicand(radicand'length - 1 downto W - 2)), W + 2);
                            state   <= S_BUSY;
                        end if;
                    when S_BUSY =>
                        counter <= counter + 1;
                        if test_res_msb = '1' then -- test_res < 0
                            -- (acc, x) <= acc(acc'length - 3 downto 0) & x & "00";
                            acc <= acc(acc'length - 3 downto 0) & x(W - 3 downto W - 4);
                        else
                            -- (acc, x) <= test_res(acc'length - 3 downto 0) & x & "00";
                            acc <= test_res(acc'length - 3 downto 0) & x(W - 3 downto W - 4);
                        end if;
                        q <= q(q'length - 2 downto 0) & not test_res_msb;
                        x <= unsigned(x(W - 5 downto 0)) & "00";
                        if counter = ITER - 1 then
                            state <= S_DONE;
                        end if;
                    when S_DONE =>
                        if root_ready then
                            state <= S_IDLE;
                        end if;
                end case;
            end if;
        end if;
    end process;
end architecture;
