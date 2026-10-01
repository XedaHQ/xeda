/* Register before VVP loads its builtin task definitions. No end callback proves finish.
 * The CI capability gate compares these calls with the engine's native batch control flow.
 */
#include <vpi_user.h>
#include <stdint.h>
#include "sim_record.h"

static const char* xeda_icarus_unit(int precision) {
    static const char* units[] = {
        "1fs", "10fs", "100fs", "1ps", "10ps", "100ps", "1ns", "10ns", "100ns",
        "1us", "10us", "100us", "1ms", "10ms", "100ms", "1s", "10s", "100s"
    };
    return precision >= -15 && precision <= 2 ? units[precision + 15] : NULL;
}

static PLI_INT32 xeda_icarus_call(PLI_BYTE8* task) {
    vpiHandle call = vpi_handle(vpiSysTfCall, NULL);
    vpiHandle args = vpi_iterate(vpiArgument, call);
    int level = 1;
    int supplied = args != NULL;
    if (args) {
        s_vpi_value value;
        vpiHandle first = vpi_scan(args);
        value.format = vpiIntVal;
        vpi_get_value(first, &value);
        level = value.value.integer;
        vpi_free_object(args);
    }
    s_vpi_time now;
    now.type = vpiSimTime;
    vpi_get_time(NULL, &now);
    uint64_t time = ((uint64_t)now.high << 32) | now.low;
    const char* unit = xeda_icarus_unit(vpi_get(vpiTimePrecision, NULL));
    const int stop = strcmp(task, "$stop") == 0;
    char record[384];
    if (!unit) exit(1);
    snprintf(record, sizeof(record),
        "{\"ended_by\":\"%s\",\"time\":%llu,\"time_unit\":\"%s\","
        "\"events\":[{\"kind\":\"%s\",\"time\":%llu}]}",
        stop ? "unknown" : "finish", (unsigned long long)time, unit,
        stop ? "stop" : "finish", (unsigned long long)time);
    if (!xeda_sim_write_record(getenv("XEDA_END_RECORD"), record)) {
        fprintf(stderr, "xeda: cannot write Icarus task evidence\n");
        exit(1);
    }
    if (level < 0 || level > 2) {
        vpi_printf("WARNING: %s:%d: %s(%d) argument must be 0, 1, or 2.\n",
            vpi_get_str(vpiFile, call), (int)vpi_get(vpiLineNo, call), task, level);
    }
    if (level) {
        vpi_printf("%s:%d: %s", vpi_get_str(vpiFile, call), (int)vpi_get(vpiLineNo, call), task);
        if (supplied) vpi_printf("(%d)", level);
        vpi_printf(" called at %llu (%s)\n", (unsigned long long)time, unit);
    }
    if (stop) vpi_control(vpiStop, level);
    else {
        vpip_set_return_value(0);
        vpi_control(vpiFinish, level);
    }
    return 0;
}

static PLI_INT32 xeda_icarus_validate(PLI_BYTE8* task) {
    vpiHandle args = vpi_iterate(vpiArgument, vpi_handle(vpiSysTfCall, NULL));
    if (args) {
        vpi_scan(args);
        if (vpi_scan(args)) {
            vpi_printf("ERROR: xeda: %s accepts at most one diagnostic-level argument.\n", task);
            vpip_set_return_value(1);
            vpi_control(vpiFinish, 1);
        }
    }
    return 0;
}

static PLI_INT32 xeda_icarus_start(p_cb_data callback) {
    (void)callback;
    setvbuf(stdout, NULL, _IOLBF, BUFSIZ);
    vpi_printf("XEDA_ICARUS_RUNTIME_START\n");
    return 0;
}

static void xeda_icarus_register(void) {
    s_vpi_systf_data task;
    memset(&task, 0, sizeof(task));
    task.type = vpiSysTask;
    task.calltf = xeda_icarus_call;
    task.compiletf = xeda_icarus_validate;
    task.tfname = "$finish";
    task.user_data = "$finish";
    vpi_register_systf(&task);
    task.tfname = "$stop";
    task.user_data = "$stop";
    vpi_register_systf(&task);
    s_cb_data callback;
    memset(&callback, 0, sizeof(callback));
    callback.reason = cbStartOfSimulation;
    callback.cb_rtn = xeda_icarus_start;
    vpi_register_cb(&callback);
}

void (*vlog_startup_routines[])(void) = {xeda_icarus_register, NULL};
