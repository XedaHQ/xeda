/* Current-run native records. Callers supply prepared paths in the runtime environment.
 * JSON is supplied by the adapter; this helper only handles checked, link-safe writes.
 * An unsuccessful write must never be treated as completion evidence.
 */
#ifndef XEDA_SIM_RECORD_H
#define XEDA_SIM_RECORD_H

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static inline int xeda_sim_write_all(int fd, const char *text) {
    size_t remaining = strlen(text);
    while (remaining) {
        ssize_t written = write(fd, text, remaining);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return 0;
        text += written;
        remaining -= (size_t)written;
    }
    return 1;
}

static inline int xeda_sim_regular(int fd) {
    struct stat state;
    return fstat(fd, &state) == 0 && S_ISREG(state.st_mode);
}

static inline int xeda_sim_append_event(const char *path, const char *json) {
    if (!path || !*path || !json) return 0;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_NONBLOCK, 0600);
    if (fd < 0) return 0;
    int ok = xeda_sim_regular(fd) && xeda_sim_write_all(fd, json) && xeda_sim_write_all(fd, "\n");
    if (close(fd) != 0) ok = 0;
    return ok;
}

static inline int xeda_sim_write_record(const char *path, const char *json) {
    if (!path || !*path || !json) return 0;
    size_t length = strlen(path);
    char *temporary = (char *)malloc(length + 5);
    if (!temporary) return 0;
    memcpy(temporary, path, length);
    memcpy(temporary + length, ".tmp", 5);
    /* Exclusive creation rejects every leftover temporary, including a dangling link. */
    int fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    int ok = 0;
    if (fd >= 0) {
        ok = xeda_sim_regular(fd) && xeda_sim_write_all(fd, json) && xeda_sim_write_all(fd, "\n");
        if (close(fd) != 0) ok = 0;
        /* Rename replaces a final link itself; it never writes through it. */
        if (ok && rename(temporary, path) != 0) ok = 0;
        /* Keep a failed partial temporary so a later writer cannot hide the failure. */
    }
    free(temporary);
    return ok;
}

#endif
