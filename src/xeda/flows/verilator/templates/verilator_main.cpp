// xeda: the driver of a Verilator model, in place of Verilator's own `--main`.
//
// It simulates as `--main` does -- evaluate, then advance to the next time slot, until `$finish`
// or until nothing is left to simulate -- and says how the simulation ended in xeda's end record
// (xeda_hooks.cpp): `finish` ($finish), `error` (a $stop that did not abort), `drained` (no event
// left, and no $finish), or `stop_time` (the stop time xeda was asked for, reached).
//
// Arguments, besides Verilator's own (`+verilator+...`):
//   +xeda+end_record+<path>   where the end record goes
//   +xeda+stop_time_ps+<N>    stop at N picoseconds of simulated time
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>

#include "verilated.h"
#include "xeda_hooks.h"
#include "{{ prefix }}.h"

namespace {

// `ps` picoseconds in ticks of a time precision of 10^precision seconds (-12 is 1ps): a whole
// number of ticks, rounded down for a precision coarser than a picosecond.
uint64_t picoseconds_to_ticks(uint64_t ps, int precision) {
    uint64_t ticks = ps;
    for (int p = precision; p < -12; ++p) ticks *= 10;  // finer than a picosecond
    for (int p = precision; p > -12; --p) ticks /= 10;  // coarser
    return ticks;
}

// The text after `+<prefix>` of the first argument that starts with it, or null.
const char* plus_argument(VerilatedContext* contextp, const char* prefix) {
    const char* const match = contextp->commandArgsPlusMatch(prefix);
    if (!match || !*match) return nullptr;
    return match + 1 + std::strlen(prefix);
}

}  // namespace

int main(int argc, char** argv, char**) {
    Verilated::debug(0);
    const std::unique_ptr<VerilatedContext> contextp{new VerilatedContext};
#if VM_TRACE
    contextp->traceEverOn(true);
#endif
    contextp->threads({{ threads }});
    contextp->commandArgs(argc, argv);

    // each read at once: the text a match returns lasts until the next match
    if (const char* const path = plus_argument(contextp.get(), "xeda+end_record+")) {
        xeda_set_record_path(path);
    }
    bool stop = false;
    uint64_t stop_ps = 0;
    if (const char* const value = plus_argument(contextp.get(), "xeda+stop_time_ps+")) {
        stop = true;
        stop_ps = std::strtoull(value, nullptr, 10);
    }

    const std::unique_ptr<{{ prefix }}> topp{new {{ prefix }}{contextp.get(), ""}};
    // the model sets the time precision when it is constructed
    const uint64_t stop_ticks = stop ? picoseconds_to_ticks(stop_ps, contextp->timeprecision()) : 0;
    if (stop) xeda_set_stop_ticks(stop_ticks);

    const char* ended_by = nullptr;
    while (true) {
        topp->eval();
        if (contextp->gotFinish()) {
            ended_by = contextp->gotError() ? "error" : "finish";
            break;
        }
        if (!topp->eventsPending()) {
            // nothing is left to simulate: at the stop time, that is the stop asked for
            ended_by = stop && contextp->time() >= stop_ticks ? "stop_time" : "drained";
            break;
        }
        const uint64_t next = topp->nextTimeSlot();
        if (stop && next > stop_ticks) {
            contextp->time(stop_ticks);
            ended_by = "stop_time";
            break;
        }
        contextp->time(next);
    }

    topp->final();
    contextp->statsPrintSummary();
    xeda_write_record(ended_by);
    return 0;
}
