/* Linked directly, so every supported main signature observes the same execution record.
 * User drivers own scheduling: no time or clock precision is invented here.
 */
#include "cxxrtl_evidence.h"
#include "sim_record.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {
[[noreturn]] void record_failure() {
    std::fputs("Cannot write CXXRTL simulation evidence\n", stderr);
    std::_Exit(1);
}

std::string quote(const char *text) {
    std::string result = "\"";
    for (const unsigned char *p = reinterpret_cast<const unsigned char *>(text); *p; ++p) {
        if (*p == '\\' || *p == '"') {
            result += '\\';
            result += static_cast<char>(*p);
        } else if (*p < 32) {
            char escaped[7];
            std::snprintf(escaped, sizeof(escaped), "\\u%04x", *p);
            result += escaped;
        } else {
            result += static_cast<char>(*p);
        }
    }
    return result + '"';
}

void record_exit() {
    if (!xeda_sim_write_record(std::getenv("XEDA_CXXRTL_END_RECORD"),
                               "{\"ended_by\":\"exit\",\"events\":[]}"))
        record_failure();
}

struct Monitor {
    Monitor() {
        std::setvbuf(stdout, nullptr, _IOLBF, BUFSIZ);
        const char *events = std::getenv("XEDA_CXXRTL_EVENTS");
        if (!events || !*events) record_failure();
        // A required empty event file proves initialization and prevents a missing file
        // from being interpreted as an absence of errors. Never open through a final link.
        int fd = open(events, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK, 0600);
        if (fd < 0) record_failure();
        int ok = xeda_sim_regular(fd);
        if (close(fd) != 0) ok = 0;
        if (!ok || std::atexit(record_exit) != 0) record_failure();
    }
} monitor;
} // namespace

void xeda_cxxrtl_assert(const char *condition, const char *file, int line) {
    const std::string location = std::string(file) + ":" + std::to_string(line);
    const std::string event = "{\"kind\":\"error\",\"location\":" + quote(location.c_str()) +
        ",\"message\":" + quote(condition) + "}";
    if (!xeda_sim_append_event(std::getenv("XEDA_CXXRTL_EVENTS"), event.c_str()))
        record_failure();
    std::fprintf(stderr, "%s: RTL assertion failed: %s\n", location.c_str(), condition);
    const char *severity = std::getenv("XEDA_CXXRTL_FAIL_SEVERITY");
    if (!severity || std::strcmp(severity, "warning") == 0 || std::strcmp(severity, "error") == 0)
        std::exit(1);
}
