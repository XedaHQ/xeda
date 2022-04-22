--===============================================================================================--
--! @file              utils_pkg.vhdl
--! @brief             Utility package
--! @author            Kamyar Mohajerani
--! @copyright         Copyright (c) 2022 Kamyar Mohajerani
--!
--! @license           Solderpad Hardware License v2.1 ([SHL-2.1](https://solderpad.org/licenses/SHL-2.1/))
--! @vhdl              VHDL 2008 and later
--!
--! @details           collection of utility functions, procedures, type definitions, etc.
--!
--===============================================================================================--

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package utils_pkg is
    -- types
    type T_UNSIGNED_ARRAY is array (natural range <>) of unsigned;

    -- functions
    function log2ceil(n : natural) return natural;
    function sum_bits(slv : std_logic_vector) return unsigned;

end package;

package body utils_pkg is
    --! Returns the number of bits required to represet values less than n (0 to n - 1 inclusive)
    function log2ceil(n : natural) return natural is
        variable r : natural := 0;
    begin
        while n > 2 ** r loop
            r := r + 1;
        end loop;
        return r;
    end function;

    --! Returns the sum of all bits in `slv`
    function sum_bits(slv : std_logic_vector) return unsigned is
        variable sum : unsigned(log2ceil(slv'length + 1) - 1 downto 0) := (others => '0');
    begin
        for i in slv'range loop
            sum := sum + slv(i);
        end loop;
        return sum;
    end function;

end package body;
