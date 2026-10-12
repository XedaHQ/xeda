// Xeda: declaration for Xeda's Verilator runtime hooks (xeda_hooks.cpp).
//
// Force-included into every C++ compile of the model (`-CFLAGS "-include xeda_hooks.h"`): with
// `VL_USER_STOP_MAYBE`, verilated.cpp calls `vl_stop_maybe` with no declaration of it in scope,
// which this header supplies. A C compile sees nothing of it.
#pragma once
#ifdef __cplusplus

extern void vl_stop_maybe(const char* filename, int linenum, const char* hier, bool maybe);

#endif  // __cplusplus
