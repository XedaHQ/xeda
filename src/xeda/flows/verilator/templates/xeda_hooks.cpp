// xeda: Verilator's overridable runtime hooks, and the end record of the simulation.
//
// Built with -DVL_USER_FINISH -DVL_USER_STOP -DVL_USER_FATAL -DVL_USER_WARN -DVL_USER_STOP_MAYBE,
// so verilated.cpp leaves these functions to this file. Each records what happened, with its
// simulated time, and then does what Verilator's own does (verilated.cpp), so the simulation
// behaves as without them.
//
// The end record (JSON) says how the simulation ended:
//   {"ended_by": "finish|stop_time|drained|error|fatal|exit", "time": T, "time_unit": "1ps",
//    "stop_ticks": S, "exit_code": null, "events": [{"kind": ..., "file": ..., "line": ...,
//    "time": ..., "msg": ...}, ...]}
// Times are in ticks of the model's time precision (`time_unit`). `$error`, a failed assertion
// and `$stop` reach `vl_stop_maybe` with `maybe` true, `$fatal` with `maybe` false: they are
// recorded as `stop_maybe` events with `maybe`, and the reader classifies them (error, fatal).
// A report that ends the simulation from `vl_stop_maybe` (through `vl_stop` and `vl_fatal`) is
// recorded once. xeda's driver writes the record at the end; `vl_fatal` writes it before the
// process exits; otherwise, when the process exits with no record written (a design's own
// driver returns), it is written at exit with `ended_by` "exit" and no time: the driver's exit
// status is known only to whoever started it.
//
// The record is written complete to `<record>.tmp`, opened without following a symbolic link,
// and then renamed over `<record>`: a rename replaces whatever is at that name, a link as
// itself.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <fcntl.h>
#ifdef _WIN32
#include <io.h>
#else
#include <unistd.h>
#endif

#include "verilated.h"
#include "xeda_hooks.h"

#ifndef O_NOFOLLOW
#define O_NOFOLLOW 0
#endif
#ifndef O_BINARY
#define O_BINARY 0
#endif

const char* xeda_ended_by = nullptr;

namespace {

struct Event {
    std::string kind;
    int maybe;  // for "stop_maybe": 1 or 0; -1 for any other kind
    std::string file;
    int line;
    uint64_t time;
    std::string msg;
};

std::vector<Event> g_events;
std::string g_record_path;
bool g_record_path_set = false;
bool g_stop_requested = false;
uint64_t g_stop_ticks = 0;
// The simulated time and the time precision, as last read from the running model. The
// VerilatedContext may be gone by the time the process exits, so it is never read there.
bool g_clock_read = false;
uint64_t g_time = 0;
std::string g_time_unit;
// Set while `vl_stop_maybe` hands a report it recorded on to `vl_stop`/`vl_fatal`.
bool g_reported = false;

void read_clock() {
    const VerilatedContext* const contextp = Verilated::threadContextp();
    g_time = contextp->time();
    g_time_unit = contextp->timeprecisionString();
    g_clock_read = true;
}

void note_event(const char* kind, int maybe, const char* filename, int linenum, const char* msg) {
    read_clock();
    g_events.push_back(
        Event{kind, maybe, filename ? filename : "", linenum, g_time, msg ? msg : ""});
}

std::string json_string(const std::string& text) {
    std::string out = "\"";
    for (const char ch : text) {
        const unsigned char code = static_cast<unsigned char>(ch);
        if (ch == '"' || ch == '\\') {
            out += '\\';
            out += ch;
        } else if (code < 0x20) {
            char escaped[8];
            std::snprintf(escaped, sizeof(escaped), "\\u%04x", code);
            out += escaped;
        } else {
            out += ch;
        }
    }
    return out + "\"";
}

std::string record_path() {
    if (g_record_path_set) return g_record_path;
    const char* const path = std::getenv("XEDA_END_RECORD");
    return path ? path : "";
}

bool write_all(int fd, const std::string& text) {
    const char* data = text.data();
    size_t left = text.size();
    while (left > 0) {
        const auto written = ::write(fd, data, static_cast<unsigned>(left));
        if (written < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        data += written;
        left -= static_cast<size_t>(written);
    }
    return true;
}

void write_record(const char* ended_by, bool with_time) {
    xeda_ended_by = ended_by;
    const std::string path = record_path();
    if (path.empty()) return;
    std::string text = "{\"ended_by\":" + json_string(ended_by);
    text += ",\"time\":" + (with_time && g_clock_read ? std::to_string(g_time) : "null");
    text += ",\"time_unit\":" + (g_clock_read ? json_string(g_time_unit) : "null");
    text += ",\"stop_ticks\":" + (g_stop_requested ? std::to_string(g_stop_ticks) : "null");
    text += ",\"exit_code\":null,\"events\":[";
    for (size_t i = 0; i < g_events.size(); ++i) {
        const Event& event = g_events[i];
        text += i ? "," : "";
        text += "{\"kind\":" + json_string(event.kind);
        if (event.maybe >= 0) text += ",\"maybe\":" + std::string(event.maybe ? "true" : "false");
        text += ",\"file\":" + json_string(event.file);
        text += ",\"line\":" + std::to_string(event.line);
        text += ",\"time\":" + std::to_string(event.time);
        text += ",\"msg\":" + json_string(event.msg) + "}";
    }
    text += "]}\n";
    const std::string temporary = path + ".tmp";
    const int fd = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_BINARY,
                          0644);
    bool written = fd >= 0 && write_all(fd, text);
    if (fd >= 0) written = (::close(fd) == 0) && written;
    if (!written || std::rename(temporary.c_str(), path.c_str()) != 0) {
        std::fprintf(stderr, "%%Warning: xeda: cannot write the end record %s: %s\n",
                     path.c_str(), std::strerror(errno));
    }
}

// At exit, when nothing wrote the record: a design's own driver has returned (or something
// called exit()). The model and its context may be destroyed by now: nothing of theirs is read.
void write_record_at_exit() {
    if (!xeda_ended_by) write_record("exit", false);
}

// Registered once every object above is constructed, so it runs before any is destroyed.
struct AtExit {
    AtExit() { std::atexit(write_record_at_exit); }
} g_at_exit;

// xeda copies the model's output through a pipe (into `sim.log`), where the C runtime buffers
// stdout in blocks: what the simulation printed before a time limit, a Ctrl-C or a crash stopped
// it would be lost, and the rest shown late. Line-buffered, as on a terminal, each line is out
// once printed -- in xeda's driver and a design's own alike, before either prints anything.
struct LineBufferedStdout {
    LineBufferedStdout() {
#ifdef _WIN32
        std::setvbuf(stdout, nullptr, _IONBF, 0);  // its C runtime has no line buffering
#else
        std::setvbuf(stdout, nullptr, _IOLBF, BUFSIZ);
#endif
    }
} g_line_buffered_stdout;

// Verilator's own message format (vl_print_warn_error in verilated.cpp): a message
// "CODE: text" prints as "<prefix>-CODE: <file>:<line>: text".
void print_message(const char* prefix, const char* filename, int linenum, const char* msg) {
    if (!msg) msg = "";
    const char* text = msg;
    while (*text >= 'A' && *text <= 'Z') ++text;
    std::string code;
    if (text[0] == ':' && text[1] == ' ') {
        code = "-" + std::string(msg, static_cast<size_t>(text - msg));
        text += 2;
    } else {
        text = msg;
    }
    if (filename && filename[0]) {
        VL_PRINTF("%s%s: %s:%d: %s\n", prefix, code.c_str(), filename, linenum, text);
    } else {
        VL_PRINTF("%s%s: %s\n", prefix, code.c_str(), text);
    }
}

}  // namespace

void xeda_set_record_path(const char* path) {
    g_record_path = path ? path : "";
    g_record_path_set = true;
}

void xeda_set_stop_ticks(uint64_t ticks) {
    g_stop_requested = true;
    g_stop_ticks = ticks;
}

void xeda_note(const char* kind, const char* filename, int linenum, const char* msg) {
    note_event(kind, -1, filename, linenum, msg);
}

void xeda_write_record(const char* ended_by) {
    read_clock();
    write_record(ended_by, true);
}

// ---- Verilator's hooks: each records, then keeps Verilator's own behavior ----

void vl_finish(const char* filename, int linenum, const char* hier) {
    (void)hier;
    note_event("finish", -1, filename, linenum, "");
    VL_PRINTF("- %s:%d: Verilog $finish\n", filename, linenum);
    Verilated::threadContextp()->gotFinish(true);
}

void vl_warn(const char* filename, int linenum, const char* hier, const char* msg) {
    (void)hier;
    note_event("warning", -1, filename, linenum, msg);
    print_message("%Warning", filename, linenum, msg);
    Verilated::runFlushCallbacks();
}

void vl_fatal(const char* filename, int linenum, const char* hier, const char* msg) {
    (void)hier;
    VerilatedContext* const contextp = Verilated::threadContextp();
    if (!g_reported) note_event("fatal", -1, filename, linenum, msg);
    contextp->gotError(true);
    contextp->gotFinish(true);
    print_message("%Error", filename, linenum, msg);
    Verilated::runFlushCallbacks();
    VL_PRINTF("Aborting...\n");
    Verilated::runFlushCallbacks();
    xeda_write_record("fatal");  // exit() below skips the driver's own end
    Verilated::runExitCallbacks();
    // as recent Verilator releases do (older ones always abort)
    if (Verilated::debug()) {
        std::abort();
    } else {
        std::exit(1);
    }
}

void vl_stop(const char* filename, int linenum, const char* hier) {  // $stop and $fatal
    VerilatedContext* const contextp = Verilated::threadContextp();
#if VERILATOR_VERSION_INTEGER >= 5048000  // a $stop after $finish, outside `final`, is ignored
    if (contextp->gotFinish() && !contextp->executingFinal()) return;
#endif
    const char* const msg = "Verilog $stop";
    if (!g_reported) note_event("stop", -1, filename, linenum, msg);
    contextp->gotError(true);
    contextp->gotFinish(true);
    if (contextp->fatalOnError()) {
        vl_fatal(filename, linenum, hier, msg);
    } else {
        print_message("%Error", filename, linenum, msg);
        Verilated::runFlushCallbacks();
    }
}

void vl_stop_maybe(const char* filename, int linenum, const char* hier, bool maybe) {
    VerilatedContext* const contextp = Verilated::threadContextp();
    contextp->errorCountInc();
    note_event("stop_maybe", maybe ? 1 : 0, filename, linenum, "");
    if (maybe && contextp->errorCount() < contextp->errorLimit()) {
        if (contextp->errorCount() == 1) {  // once, as the error limit is crossed
            print_message("-Info", filename, linenum,
                          "Verilog $stop, ignored due to +verilator+error+limit");
        }
    } else {
        g_reported = true;
        vl_stop(filename, linenum, hier);
        g_reported = false;
    }
}
