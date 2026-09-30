// xeda: declarations for xeda's Verilator runtime hooks (xeda_hooks.cpp).
//
// Force-included into every C++ compile of the model (`-CFLAGS "-include xeda_hooks.h"`): with
// `VL_USER_STOP_MAYBE`, verilated.cpp calls `vl_stop_maybe` with no declaration of it in scope,
// which this header supplies. A C compile sees nothing of it.
#pragma once
#ifdef __cplusplus

#include <cstdint>

extern void vl_stop_maybe(const char* filename, int linenum, const char* hier, bool maybe);

// What xeda's driver (verilator_main.cpp) calls; defined in xeda_hooks.cpp.
extern "C++" {
// What the end record says ended the simulation; null until the record is written.
extern const char* xeda_ended_by;
// Where the end record goes. Unset, the hooks read it from the environment variable
// XEDA_END_RECORD (a design's own driver takes no arguments of xeda's); unset there too, no
// record is written.
void xeda_set_record_path(const char* path);
// The stop time xeda's driver was asked for, in ticks of the model's time precision.
void xeda_set_stop_ticks(uint64_t ticks);
// Record an event of `kind` at the current simulated time.
void xeda_note(const char* kind, const char* filename, int linenum, const char* msg);
// Write the end record: what ended the simulation, at the current simulated time.
void xeda_write_record(const char* ended_by);
}

#endif  // __cplusplus
