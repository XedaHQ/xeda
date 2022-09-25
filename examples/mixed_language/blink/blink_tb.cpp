#include <fstream>
#include <iostream>

#include <backends/cxxrtl/cxxrtl_vcd.h>

#include "blink.h"

int main() {
    cxxrtl_design::p_blink top;
    
    auto vcd_filename = "waves.vcd";

    std::cout << "dumbping waveform to " << vcd_filename << std::endl;

    // debug_items maps the hierarchical names of signals and memories in the
    // design to a cxxrtl_object (a value, a wire, or a memory)
    cxxrtl::debug_items all_debug_items;

    // Load the debug items of the top down the whole design hierarchy
    top.debug_info(all_debug_items);

    // vcd_writer is the CXXRTL object that's responsible of creating a string
    // with the VCD file contents.
    cxxrtl::vcd_writer vcd;
    vcd.timescale(1, "ns");

    // Here we tell the vcd writer to dump all the signals of the design, except
    // for the memories, to the VCD file.
    //
    // It's not necessary to load all debug objects to the VCD. There is, for
    // example, a  vcd.add(<debug items>, <filter>)) method which allows
    // creating your custom filter to decide what to add and what not.
    vcd.add_without_memories(all_debug_items);

    std::ofstream waves(vcd_filename);

    bool prev_led = 0;

    /// Is only a single-threaded sequential driver/monitor simulation supported?!

    for (int step = 0; step < 1000; ++step) {

        top.p_clk.set(false);
        top.step();
        vcd.sample(step * 2 + 0);

        top.p_clk.set(true);
        top.step();
        vcd.sample(step * 2 + 1);

        if (step < 2){
            top.p_reset.set(true);
            continue;
        } else {
            top.p_reset.set(false);
        }

        bool cur_led = top.p_led.get<bool>();
        uint32_t counter = top.p_counter.get<uint32_t>();

        if (cur_led != prev_led) {
            std::cout << "cycle " << step << " - led: " << cur_led
                 << ", counter: " << counter << std::endl;
        }
        prev_led = cur_led;

        waves << vcd.buffer;
        vcd.buffer.clear();
    }
}