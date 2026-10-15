// Xeda: declaration for Xeda's Verilator runtime hooks (xeda_hooks.cpp).
//
// With `VL_USER_STOP_MAYBE`, verilated.cpp calls `vl_stop_maybe` with no declaration of it in
// scope, which this header supplies. xeda_hooks.mk forces it into the compile of verilated.cpp
// only: forced into the model's compiles, it would keep GCC from using the model's precompiled
// header. A C compile sees nothing of it.
#pragma once
#ifdef __cplusplus

extern void vl_stop_maybe(const char* filename, int linenum, const char* hier, bool maybe);

#endif  // __cplusplus
