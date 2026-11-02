/* Register before VVP loads its built-in task definitions. The end callback does not prove that
 * the simulation finished, so it records an unknown end. The CI capability gate compares these
 * calls with the native batch control flow of the engine.
 */
#include <vpi_user.h>
#include <stdint.h>
#include "sim_record.h"

static int xeda_icarus_recorded = 0;

static const char* xeda_icarus_unit(int precision) {
    static const char* units[] = {
        "1fs", "10fs", "100fs", "1ps", "10ps", "100ps", "1ns", "10ns", "100ns",
        "1us", "10us", "100us", "1ms", "10ms", "100ms", "1s", "10s", "100s"
    };
    return precision >= -15 && precision <= 2 ? units[precision + 15] : NULL;
}

static unsigned long long xeda_icarus_now(void) {
    s_vpi_time now;
    now.type = vpiSimTime;
    vpi_get_time(NULL, &now);
    return ((unsigned long long)now.high << 32) | now.low;
}

/* Keep the first end of the run: `event` is the kind of its one event, or NULL for none. */
static void xeda_icarus_record(const char* ended_by, const char* event) {
    if (xeda_icarus_recorded) return;
    xeda_icarus_recorded = 1;
    unsigned long long time = xeda_icarus_now();
    const char* unit = xeda_icarus_unit(vpi_get(vpiTimePrecision, NULL));
    char events[64] = "";
    char record[384];
    if (!unit) exit(1);
    if (event) snprintf(events, sizeof(events), "{\"kind\":\"%s\",\"time\":%llu}", event, time);
    snprintf(record, sizeof(record),
        "{\"ended_by\":\"%s\",\"time\":%llu,\"time_unit\":\"%s\",\"events\":[%s]}",
        ended_by, time, unit, events);
    if (!xeda_sim_write_record(getenv("XEDA_END_RECORD"), record)) {
        fprintf(stderr, "xeda: cannot write Icarus task evidence\n");
        exit(1);
    }
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
    const int stop = strcmp(task, "$stop") == 0;
    xeda_icarus_record(stop ? "unknown" : "finish", stop ? "stop" : "finish");
    if (level < 0 || level > 2) {
        vpi_printf("WARNING: %s:%d: %s(%d) argument must be 0, 1, or 2.\n",
            vpi_get_str(vpiFile, call), (int)vpi_get(vpiLineNo, call), task, level);
    }
    if (level) {
        vpi_printf("%s:%d: %s", vpi_get_str(vpiFile, call), (int)vpi_get(vpiLineNo, call), task);
        if (supplied) vpi_printf("(%d)", level);
        vpi_printf(" called at %llu (%s)\n", xeda_icarus_now(),
            xeda_icarus_unit(vpi_get(vpiTimePrecision, NULL)));
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

static PLI_INT32 xeda_icarus_stop_time(p_cb_data callback) {
    (void)callback;
    xeda_icarus_record("stop_time", NULL);
    vpi_control(vpiFinish, 0);
    return 0;
}

/* `femtoseconds` is a decimal number, and a tick is a power of ten: dropping digits divides. */
static void xeda_icarus_schedule_stop(const char* femtoseconds) {
    size_t digits = strlen(femtoseconds);
    size_t dropped = (size_t)(vpi_get(vpiTimePrecision, NULL) + 15);
    char ticks[32] = "0";
    if (digits > dropped && digits - dropped < sizeof(ticks)) {
        memcpy(ticks, femtoseconds, digits - dropped);
        ticks[digits - dropped] = '\0';
    }
    errno = 0;
    unsigned long long count = strtoull(ticks, NULL, 10);
    if (errno || (digits > dropped && digits - dropped >= sizeof(ticks))) {
        vpi_printf("ERROR: xeda: stop_time %s fs does not fit the simulated time.\n", femtoseconds);
        vpip_set_return_value(1);
        vpi_control(vpiFinish, 1);
        return;
    }
    s_vpi_time delay;
    memset(&delay, 0, sizeof(delay));
    delay.type = vpiSimTime;
    delay.high = (PLI_UINT32)(count >> 32);
    delay.low = (PLI_UINT32)count;
    s_cb_data callback;
    memset(&callback, 0, sizeof(callback));
    callback.reason = cbAfterDelay;
    callback.time = &delay;
    callback.cb_rtn = xeda_icarus_stop_time;
    vpi_register_cb(&callback);
}

static PLI_INT32 xeda_icarus_start(p_cb_data callback) {
    (void)callback;
    setvbuf(stdout, NULL, _IOLBF, BUFSIZ);
    vpi_printf("XEDA_ICARUS_RUNTIME_START\n");
    const char* stop_time = getenv("XEDA_STOP_TIME_FS");
    if (stop_time && *stop_time) xeda_icarus_schedule_stop(stop_time);
    return 0;
}

static PLI_INT32 xeda_icarus_end(p_cb_data callback) {
    (void)callback;
    xeda_icarus_record("unknown", NULL);
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
    callback.reason = cbEndOfSimulation;
    callback.cb_rtn = xeda_icarus_end;
    vpi_register_cb(&callback);
}

void (*vlog_startup_routines[])(void) = {xeda_icarus_register, NULL};
