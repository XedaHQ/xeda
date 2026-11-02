/* Passive observation only: no delayed callback, clock, or keep-alive event. */
#include <vhpi_user.h>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include "sim_record.h"

static uint64_t ticks(const vhpiTimeT& time) {
    return (uint64_t(uint32_t(time.high)) << 32) | time.low;
}

static void started(const vhpiCbDataT*) {
    std::fprintf(stderr, "XEDA_NVC_RUNTIME_START\n");
    std::fflush(stderr);
}

static void ended(const vhpiCbDataT*) {
    vhpiTimeT time = {}, next = {};
    long delta = 0;
    vhpi_get_time(&time, &delta);
    int status = vhpi_get_next_time(&next);
    if (status != 0 && status != vhpiNoActivity) {
        std::fprintf(stderr, "XEDA: NVC cannot observe pending simulation activity\n");
        return;
    }
    char pending[32], record[192];
    if (status == vhpiNoActivity) std::snprintf(pending, sizeof pending, "null");
    else std::snprintf(pending, sizeof pending, "%" PRIu64, ticks(next));
    std::snprintf(record, sizeof record,
                  "{\"time\":%" PRIu64 ",\"time_unit\":\"1fs\",\"next_time\":%s}",
                  ticks(time), pending);
    if (!xeda_sim_write_record(std::getenv("XEDA_NVC_END_RECORD"), record))
        std::fprintf(stderr, "XEDA: NVC cannot write end checkpoint\n");
}

static void startup() {
    /* Preserve HDL output written before a wall-clock timeout kills the process. */
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    vhpiCbDataT start = {}, end = {};
    start.reason = vhpiCbStartOfSimulation;
    start.cb_rtn = started;
    end.reason = vhpiCbEndOfSimulation;
    end.cb_rtn = ended;
    if (!vhpi_register_cb(&start, vhpiReturnCb) || !vhpi_register_cb(&end, vhpiReturnCb)) {
        std::fprintf(stderr, "XEDA: NVC cannot register passive simulation callbacks\n");
        vhpi_control(vhpiFinish, 1);
    }
}

extern "C" {
void (*vhpi_startup_routines[])() = {startup, nullptr};
}
