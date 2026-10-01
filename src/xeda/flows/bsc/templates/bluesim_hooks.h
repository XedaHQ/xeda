// Include native declarations before intercepting generated task calls.
#ifndef XEDA_BLUESIM_HOOKS_H
#define XEDA_BLUESIM_HOOKS_H
#include "bluesim_primitives.h"
#include "bluesim_kernel_api.h"
#include "bs_system_tasks.h"
#include "sim_record.h"
#include <cstdio>
#include <cstdlib>

template<class... Args>
static inline void xeda_bluesim_note(const char* kind, tSimStateHdl hdl, Args... args) {
    (void)sizeof...(args);
    char record[192];
    std::snprintf(record, sizeof(record), "{\"kind\":\"%s\",\"time\":%llu}",
                  kind, (unsigned long long)bk_now(hdl));
    if (!xeda_sim_append_event(std::getenv("XEDA_SIM_EVENTS"), record)) {
        std::fprintf(stderr, "xeda: cannot write Bluesim task evidence\n");
        std::exit(1); // a missing diagnostic must never allow a successful checkpoint
    }
}
#define dollar_finish(...) (xeda_bluesim_note("finish", __VA_ARGS__), dollar_finish(__VA_ARGS__))
#define dollar_stop(...) (xeda_bluesim_note("stop", __VA_ARGS__), dollar_stop(__VA_ARGS__))
#define dollar_error(...) (xeda_bluesim_note("error", __VA_ARGS__), dollar_error(__VA_ARGS__))
#define dollar_warning(...) (xeda_bluesim_note("warning", __VA_ARGS__), dollar_warning(__VA_ARGS__))
#define dollar_fatal(...) (xeda_bluesim_note("fatal", __VA_ARGS__), dollar_fatal(__VA_ARGS__))
#endif
