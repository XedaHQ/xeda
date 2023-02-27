--===============================================================================================--
--! @file              trivium.vhd
--! @brief             Trivium Cipher
--! @author            Kamyar Mohajerani
--! @copyright         Copyright (c) 2022
--! @license           Solderpad Hardware License v2.1 ([SHL-2.1](https://solderpad.org/licenses/SHL-2.1/))
--!
--! @vhdl              VHDL 2008, and later
--!
--! @details
--! @note
--===============================================================================================--

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity trivium is
    generic(
        G_SETUP_ROUNDS : positive                    := 4; -- 4 is the standard value
        G_IN_BITS      : positive range 1 to 80      := 64; -- <= 80
        G_OUT_BITS     : positive range 1 to 4 * 288 := 64; -- must divide 3^2*2^7 (4*288)
        G_KEY_IV_BITS  : positive range 64 to 80     := 64
    );
    port(
        clk       : in  std_logic;
        rst       : in  std_logic;
        din_data  : in  std_logic_vector(G_IN_BITS - 1 downto 0);
        din_valid : in  std_logic;
        din_ready : out std_logic;
        ks_data   : out std_logic_vector(G_OUT_BITS - 1 downto 0);
        ks_valid  : out std_logic;
        ks_ready  : in  std_logic;
        -- stop keystream generation and start over (assert for one cycle when ks_valid)
        rekey     : in  std_logic
    );

end entity;

architecture RTL of trivium is
    constant TRIVIUM_SIZE : positive := 288; -- DO NOT CHANGE
    constant SETUP_CYCLES : positive := (G_SETUP_ROUNDS * TRIVIUM_SIZE) / G_OUT_BITS;
    subtype T_TRIVIUM_STATE is std_logic_vector(1 to TRIVIUM_SIZE);
    type T_STATE is (S_INIT_K, S_INIT_IV, S_SETUP, S_KEYSTREAM);

    function log2ceil(n : natural) return natural is
        variable r : natural := 0;
    begin
        while n > 2 ** r loop
            r := r + 1;
        end loop;
        return r;
    end function;

    procedure update_state(current : in T_TRIVIUM_STATE; signal nxt : out T_TRIVIUM_STATE; signal z : out std_logic_vector(G_OUT_BITS - 1 downto 0)) is
        variable t : std_logic_vector(1 to 3);
        variable s : T_TRIVIUM_STATE := current;
    begin
        for i in z'length - 1 downto 0 loop
            t    := (s(66) xor s(93)) & (s(162) xor s(177)) & (s(243) xor s(288));
            z(i) <= t(1) xor t(2) xor t(3);
            t(1) := t(1) xor (s(91) and s(92)) xor s(171);
            t(2) := t(2) xor (s(175) and s(176)) xor s(264);
            t(3) := t(3) xor (s(286) and s(287)) xor s(69);
            s    := t(3) & s(1 to 92) & t(1) & s(94 to 176) & t(2) & s(178 to 287);
        end loop;
        nxt <= s;
    end procedure;
    -- registers
    signal s       : T_TRIVIUM_STATE;
    signal state   : T_STATE;
    signal counter : unsigned(log2ceil(SETUP_CYCLES) - 1 downto 0);
    -- wires
    signal nxt_s   : T_TRIVIUM_STATE;
    signal kiv     : std_logic_vector(1 to G_IN_BITS); -- key/iv

    signal ctr : integer range 0 to G_KEY_IV_BITS / G_IN_BITS - 1;
begin

    assert SETUP_CYCLES * G_OUT_BITS = (G_SETUP_ROUNDS * TRIVIUM_SIZE) and G_IN_BITS <= 80
    report "G_OUT_BITS must divide G_SETUP_ROUNDS * 288 and G_IN_BITS must be <= 80"
    severity failure;

    process(all)
    begin
        for i in 1 to G_IN_BITS loop
            kiv(i) <= din_data(i - 1);
        end loop;
        update_state(s, nxt_s, ks_data);
        din_ready <= '0';
        ks_valid  <= '0';
        case state is
            when S_INIT_K =>
                din_ready <= '1';
            when S_INIT_IV =>
                din_ready <= '1';
            when S_SETUP =>
                null;
            when S_KEYSTREAM =>
                ks_valid <= '1';
        end case;
    end process;

    GEN_CTR : if G_KEY_IV_BITS / G_IN_BITS = 1 generate
        ctr <= 0;
    else generate
        ctr <= G_KEY_IV_BITS / G_IN_BITS - 1 - to_integer(counter(log2ceil(G_KEY_IV_BITS / G_IN_BITS) - 1 downto 0));
    end generate;

    process(clk)
    begin
        if rising_edge(clk) then
            if rst = '1' then
                counter <= (others => '0');
                state   <= S_INIT_K;
            else
                case state is
                    when S_INIT_K =>
                        if din_valid then
                            if ctr = 0 then
                                counter <= (others => '0');
                                state   <= S_INIT_IV;
                            else
                                counter <= counter + 1;
                            end if;
                        end if;
                        -- FIXME: Vivado Synthesis reports out of bound access when G_IN_BITS != G_OUT_BITS
                        s(1 + ctr * G_IN_BITS to ctr * G_IN_BITS + G_IN_BITS) <= kiv;

                        s(G_KEY_IV_BITS + 1 to 93)                <= (others => '0');
                        s(94 + G_KEY_IV_BITS to TRIVIUM_SIZE - 3) <= (others => '0');
                        s(TRIVIUM_SIZE - 2 to TRIVIUM_SIZE)       <= (others => '1');
                    when S_INIT_IV =>
                        if din_valid then
                            if ctr = 0 then
                                counter <= (others => '0');
                                state   <= S_SETUP;
                            else
                                counter <= counter + 1;
                            end if;
                        end if;
                        s(94 + ctr * G_IN_BITS to 93 + ctr * G_IN_BITS + G_IN_BITS) <= kiv;
                    when S_SETUP =>
                        counter <= counter + 1;
                        s       <= nxt_s;
                        if counter = SETUP_CYCLES - 1 then
                            counter <= (others => '0');
                            state   <= S_KEYSTREAM;
                        end if;
                    when S_KEYSTREAM =>
                        if rekey then
                            state <= S_INIT_K;
                        end if;
                        if ks_ready then
                            s <= nxt_s;
                        end if;
                end case;
            end if;
        end if;
    end process;
end architecture;
