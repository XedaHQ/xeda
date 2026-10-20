# The bootstrap script of a remote run. It runs on the remote host, as `sh <this file> <command> ...`.
# POSIX sh only: it must work in dash and in busybox ash.
#
# The installer of `xeda.remote.provision` writes this file to `R/v1/bin/boot-<hash>.sh`, where `R`
# is the remote root. Then it runs this file with these commands. Each command has the hash of the
# environment file as its first argument, and the script sources that file before it does anything.
#
#   status <env-hash> <deps-key> <code-key>
#   provision <env-hash> <deps-key> <python> <uv-version> <uv-url> <uv-sha256>
#       The standard input holds the lines `R <requirement>` and `C <constraint>`.
#   put-code <env-hash> <code-key>
#       The standard input is the tar stream of the xeda code snapshot.
#   agent <env-hash> <deps-key> <code-key> <argument>...
#
# The standard output holds the protocol lines. Every line of the protocol starts with `XEDA-`, and
# the client ignores every other line, because a login shell can print text of its own.
#   XEDA-STEP <text>
#   XEDA-STATUS env=<present|missing> code=<present|missing>
#   XEDA-ERROR <code> <text>
# The output of the tools goes to the standard error. The command `agent` is the exception: it
# replaces this script with the agent, which owns the standard input and output.

# Every variable of this script starts with XEDA_BOOT_, because the environment file of the host
# profile is sourced and its variables must never change the state of the script. The values that
# the script reads and discards are in variables that the shell sees as unused.
# shellcheck disable=SC2034
set -eu

# ---- Output ----------------------------------------------------------------------------------

# A line of the protocol. The empty line before it ends a line of noise that has no line break.
say() {
    printf '\nXEDA-%s\n' "$*"
}

die() {
    say "ERROR $1 $2"
    exit 1
}

# ---- Cleanup ---------------------------------------------------------------------------------

# Every directory and lock that this process makes is removed when it ends, also when a signal
# stops it.
XEDA_BOOT_HELD_LOCKS=
XEDA_BOOT_WORK=
XEDA_BOOT_STAGE=
XEDA_BOOT_ENV_BUILDING=
XEDA_BOOT_CHILD=

cleanup() {
    XEDA_BOOT_STATUS=$?
    trap - EXIT
    if [ -n "$XEDA_BOOT_CHILD" ]; then
        kill "$XEDA_BOOT_CHILD" 2>/dev/null || :
    fi
    if [ -n "$XEDA_BOOT_ENV_BUILDING" ]; then
        rm -rf "$XEDA_BOOT_ENV_BUILDING"
    fi
    if [ -n "$XEDA_BOOT_STAGE" ]; then
        rm -rf "$XEDA_BOOT_STAGE"
    fi
    if [ -n "$XEDA_BOOT_WORK" ]; then
        rm -rf "$XEDA_BOOT_WORK"
    fi
    for XEDA_BOOT_HELD in $XEDA_BOOT_HELD_LOCKS; do
        rm -rf "$XEDA_BOOT_LOCKS/$XEDA_BOOT_HELD.lock"
    done
    exit "$XEDA_BOOT_STATUS"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 141' PIPE
trap 'exit 143' TERM

# ---- The layout ------------------------------------------------------------------------------

# This file is `R/v1/bin/boot-<hash>.sh`, so the directory `R/v1` is two levels up.
case $0 in
    /*) ;;
    *) die bad-request "The script path '$0' is not absolute." ;;
esac
XEDA_BOOT_V=$(cd "$(dirname "$0")/.." && pwd)
XEDA_BOOT_LOCKS=$XEDA_BOOT_V/locks
# The environment file of the host profile comes first: its variables apply to everything below.
if [ $# -lt 2 ]; then
    die bad-request "Usage: boot.sh <command> <env-hash> ..."
fi
XEDA_BOOT_COMMAND=$1
XEDA_BOOT_ENV_HASH=$2
shift 2
case $XEDA_BOOT_ENV_HASH in
    '' | *[!0-9a-f]*) die bad-request "The environment hash '$XEDA_BOOT_ENV_HASH' is not a key." ;;
esac
if [ ! -f "$XEDA_BOOT_V/bin/env-$XEDA_BOOT_ENV_HASH.sh" ]; then
    die bad-request "The environment file env-$XEDA_BOOT_ENV_HASH.sh is missing."
fi
# shellcheck source=/dev/null
. "$XEDA_BOOT_V/bin/env-$XEDA_BOOT_ENV_HASH.sh"

XEDA_BOOT_HOST=$(uname -n)
#: Seconds between two checks of a lock. The tests set a shorter time in the `env` of a profile.
XEDA_BOOT_POLL=${XEDA_LOCK_POLL:-1}
#: Seconds after which a lock that another process holds is reported, not waited for.
XEDA_BOOT_LOCK_LIMIT=1800
#: Seconds after which a lock without a readable owner is broken. The process that makes a lock
#: writes its owner file at once, so an older lock has no owner that is still on its way. The
#: margin allows for clocks that differ between the machines of a shared home directory.
XEDA_BOOT_ORPHAN_GRACE=60
#: Free space in KB below which a failed step is reported as a full disk.
XEDA_BOOT_LOW_DISK_KB=51200

# ---- Helpers ---------------------------------------------------------------------------------

free_kb() {
    df -Pk "$XEDA_BOOT_V" 2>/dev/null </dev/null | {
        read -r XEDA_BOOT_HEADER || :
        read -r XEDA_BOOT_NAME XEDA_BOOT_SIZE XEDA_BOOT_USED XEDA_BOOT_AVAILABLE XEDA_BOOT_REST || :
        printf '%s' "${XEDA_BOOT_AVAILABLE:-}"
    }
}

# Report a failed step with `code`, or, if the disk is nearly full, as a full disk.
fail_step() {
    XEDA_BOOT_CODE=$1
    XEDA_BOOT_MESSAGE=$2
    XEDA_BOOT_FREE=$(free_kb)
    case $XEDA_BOOT_FREE in
        '' | *[!0-9]*) ;;
        *)
            if [ "$XEDA_BOOT_FREE" -lt "$XEDA_BOOT_LOW_DISK_KB" ]; then
                die no-space "The disk of $XEDA_BOOT_V has only $XEDA_BOOT_FREE KB free. $XEDA_BOOT_MESSAGE"
            fi
            ;;
    esac
    die "$XEDA_BOOT_CODE" "$XEDA_BOOT_MESSAGE"
}

# Run a tool with its output on the standard error and no standard input. The tool is a child
# process that a signal can stop: the shell runs the trap of a signal only after a foreground
# command ends, but `wait` ends at once.
run() {
    "$@" >&2 </dev/null &
    XEDA_BOOT_CHILD=$!
    XEDA_BOOT_RUN_STATUS=0
    wait "$XEDA_BOOT_CHILD" || XEDA_BOOT_RUN_STATUS=$?
    XEDA_BOOT_CHILD=
    return "$XEDA_BOOT_RUN_STATUS"
}

check_key() {
    case $2 in
        '' | *[!0-9a-f]*) die bad-request "The $1 '$2' is not a key." ;;
    esac
}

# The files `.xeda-complete` hold the key of their directory. A directory that has no such
# file, or another one, is not complete: the process that made it did not finish.
env_complete() {
    [ -f "$XEDA_BOOT_V/env/$1/.xeda-complete" ] \
        && [ "$(cat "$XEDA_BOOT_V/env/$1/.xeda-complete")" = "$1" ] \
        && [ -x "$XEDA_BOOT_V/env/$1/bin/python" ]
}

code_complete() {
    [ -f "$XEDA_BOOT_V/code/$1/.xeda-complete" ] \
        && [ "$(cat "$XEDA_BOOT_V/code/$1/.xeda-complete")" = "$1" ] \
        && [ -d "$XEDA_BOOT_V/code/$1/xeda" ]
}

# Make the work directory of this process, once. It is on the file system of the layout, so that
# a rename from it into the layout is atomic.
make_work() {
    if [ -z "$XEDA_BOOT_WORK" ]; then
        mkdir -p "$XEDA_BOOT_V/tmp" || fail_step env-failed "Cannot create $XEDA_BOOT_V/tmp."
        XEDA_BOOT_WORK=$XEDA_BOOT_V/tmp/$XEDA_BOOT_HOST.$$
        rm -rf "$XEDA_BOOT_WORK"
        mkdir "$XEDA_BOOT_WORK" || fail_step env-failed "Cannot create $XEDA_BOOT_WORK."
    fi
}

# ---- Locks -----------------------------------------------------------------------------------

# A lock is the directory `locks/<name>.lock` with the file `owner`: the host, the process and the
# time. A lock whose owner process is gone on this host is broken, and a lock whose owner process
# is alive on this host is waited for. This script cannot know whether a process on another host
# is alive, so it reports the lock of another host that is older than the limit. A lock without
# a readable owner is broken once its directory is older than the grace period.

process_alive() {
    kill -0 "$1" 2>/dev/null || [ -d "/proc/$1" ]
}

# The modification time of the path $1 in seconds since the epoch. The GNU and the BSD form of
# `stat` differ. A system that has neither prints nothing.
mtime_of() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || :
}

# Whether the lock directory $1 is older than the grace period. A lock without a modification
# time is not old.
lock_is_old() {
    XEDA_BOOT_MTIME=$(mtime_of "$1")
    case $XEDA_BOOT_MTIME in
        '' | *[!0-9]*) return 1 ;;
    esac
    [ $(($(date +%s) - XEDA_BOOT_MTIME)) -gt "$XEDA_BOOT_ORPHAN_GRACE" ]
}

# Break the lock directory $1 if its owner is still the one in $2, $3 and $4. Only one process
# breaks a lock at a time, and it checks the owner again, so that no process breaks a new lock.
# An owner that is not complete (no host, process or time) breaks the lock only if the lock is
# still old, because a new lock has no owner for a moment after it is made.
# The break directory holds the time it was made. One that is older than the limit belongs to a
# process that stopped, and it is removed.
break_lock() {
    XEDA_BOOT_BREAKER=$1.break
    if mkdir "$XEDA_BOOT_BREAKER" 2>/dev/null; then
        date +%s >"$XEDA_BOOT_BREAKER/time"
        XEDA_BOOT_AGAIN_HOST=
        XEDA_BOOT_AGAIN_PID=
        XEDA_BOOT_AGAIN_TIME=
        if [ -r "$1/owner" ]; then
            read -r XEDA_BOOT_AGAIN_HOST XEDA_BOOT_AGAIN_PID XEDA_BOOT_AGAIN_TIME XEDA_BOOT_REST 2>/dev/null <"$1/owner" || :
        fi
        if [ "$XEDA_BOOT_AGAIN_HOST $XEDA_BOOT_AGAIN_PID $XEDA_BOOT_AGAIN_TIME" = "$2 $3 $4" ] \
            && { [ -n "$2" ] && [ -n "$3" ] && [ -n "$4" ] || lock_is_old "$1"; }; then
            rm -rf "$1"
        fi
        rm -rf "$XEDA_BOOT_BREAKER"
    elif read -r XEDA_BOOT_BREAK_TIME 2>/dev/null <"$XEDA_BOOT_BREAKER/time"; then
        case $XEDA_BOOT_BREAK_TIME in
            '' | *[!0-9]*) ;;
            *)
                if [ $(($(date +%s) - XEDA_BOOT_BREAK_TIME)) -gt "$XEDA_BOOT_LOCK_LIMIT" ]; then
                    rm -rf "$XEDA_BOOT_BREAKER"
                fi
                ;;
        esac
    fi
}

acquire() {
    XEDA_BOOT_NAME=$1
    XEDA_BOOT_LOCK=$XEDA_BOOT_LOCKS/$XEDA_BOOT_NAME.lock
    mkdir -p "$XEDA_BOOT_LOCKS" || fail_step env-failed "Cannot create $XEDA_BOOT_LOCKS."
    XEDA_BOOT_ANNOUNCED=
    while :; do
        if mkdir "$XEDA_BOOT_LOCK" 2>/dev/null; then
            XEDA_BOOT_HELD_LOCKS="$XEDA_BOOT_HELD_LOCKS $XEDA_BOOT_NAME"
            printf '%s %s %s\n' "$XEDA_BOOT_HOST" "$$" "$(date +%s)" >"$XEDA_BOOT_LOCK/owner"
            return 0
        fi
        XEDA_BOOT_OWNER_HOST=
        XEDA_BOOT_OWNER_PID=
        XEDA_BOOT_OWNER_TIME=
        if [ -r "$XEDA_BOOT_LOCK/owner" ]; then
            read -r XEDA_BOOT_OWNER_HOST XEDA_BOOT_OWNER_PID XEDA_BOOT_OWNER_TIME XEDA_BOOT_REST 2>/dev/null <"$XEDA_BOOT_LOCK/owner" || :
        fi
        case $XEDA_BOOT_OWNER_PID:$XEDA_BOOT_OWNER_TIME in
            *[!0-9:]* | :* | *:)
                # The owner file is missing, empty or incomplete. The process that made the lock
                # may still be writing it, so the lock is broken only when it is old.
                if lock_is_old "$XEDA_BOOT_LOCK"; then
                    break_lock "$XEDA_BOOT_LOCK" "$XEDA_BOOT_OWNER_HOST" "$XEDA_BOOT_OWNER_PID" "$XEDA_BOOT_OWNER_TIME"
                    if [ ! -d "$XEDA_BOOT_LOCK" ]; then
                        continue
                    fi
                fi
                ;;
            *)
                if [ "$XEDA_BOOT_OWNER_HOST" = "$XEDA_BOOT_HOST" ]; then
                    # A process on this host is alive or not, so wait for it, or break the lock.
                    if ! process_alive "$XEDA_BOOT_OWNER_PID"; then
                        break_lock "$XEDA_BOOT_LOCK" "$XEDA_BOOT_OWNER_HOST" "$XEDA_BOOT_OWNER_PID" "$XEDA_BOOT_OWNER_TIME"
                        if [ ! -d "$XEDA_BOOT_LOCK" ]; then
                            continue
                        fi
                    fi
                else
                    XEDA_BOOT_AGE=$(($(date +%s) - XEDA_BOOT_OWNER_TIME))
                    if [ "$XEDA_BOOT_AGE" -gt "$XEDA_BOOT_LOCK_LIMIT" ]; then
                        die lock-timeout "The lock $XEDA_BOOT_LOCK has been held for $((XEDA_BOOT_AGE / 60)) minutes by process $XEDA_BOOT_OWNER_PID on $XEDA_BOOT_OWNER_HOST. If that process does not run, remove the directory."
                    fi
                fi
                ;;
        esac
        if [ -z "$XEDA_BOOT_ANNOUNCED" ]; then
            say "STEP Waiting for another xeda process ($XEDA_BOOT_NAME)"
            XEDA_BOOT_ANNOUNCED=1
        fi
        sleep "$XEDA_BOOT_POLL"
    done
}

release() {
    rm -rf "$XEDA_BOOT_LOCKS/$1.lock"
    XEDA_BOOT_KEPT=
    for XEDA_BOOT_HELD in $XEDA_BOOT_HELD_LOCKS; do
        if [ "$XEDA_BOOT_HELD" != "$1" ]; then
            XEDA_BOOT_KEPT="$XEDA_BOOT_KEPT $XEDA_BOOT_HELD"
        fi
    done
    XEDA_BOOT_HELD_LOCKS=$XEDA_BOOT_KEPT
}

# The names of the work, staging and uv directories end with `<host>.<pid>`. A directory of a
# process that is gone on this host is a leftover of a process that was killed, and it is removed.
# The host is in the name because a home directory can be shared by several hosts, and a process
# number means something on one host only.
sweep_entry() {
    XEDA_BOOT_SWEEP_PATH=$1
    XEDA_BOOT_SWEEP_PID=${2##*.}
    XEDA_BOOT_SWEEP_HOST=${2%.*}
    case $XEDA_BOOT_SWEEP_PID in
        '' | *[!0-9]*) return 0 ;;
    esac
    if [ "$XEDA_BOOT_SWEEP_HOST" = "$XEDA_BOOT_HOST" ] && ! process_alive "$XEDA_BOOT_SWEEP_PID"; then
        rm -rf "$XEDA_BOOT_SWEEP_PATH"
    fi
}

sweep_leftovers() {
    for XEDA_BOOT_ENTRY in "$XEDA_BOOT_V"/tmp/*; do
        [ -e "$XEDA_BOOT_ENTRY" ] || continue
        sweep_entry "$XEDA_BOOT_ENTRY" "${XEDA_BOOT_ENTRY##*/}"
    done
    # code/.<key>.<host>.<pid>
    for XEDA_BOOT_ENTRY in "$XEDA_BOOT_V"/code/.[0-9a-f]*; do
        [ -e "$XEDA_BOOT_ENTRY" ] || continue
        XEDA_BOOT_NAME=${XEDA_BOOT_ENTRY##*/}
        XEDA_BOOT_NAME=${XEDA_BOOT_NAME#.}
        sweep_entry "$XEDA_BOOT_ENTRY" "${XEDA_BOOT_NAME#*.}"
    done
    # uv/<version>-<target>.new.<host>.<pid>
    for XEDA_BOOT_ENTRY in "$XEDA_BOOT_V"/uv/*.new.*; do
        [ -e "$XEDA_BOOT_ENTRY" ] || continue
        XEDA_BOOT_NAME=${XEDA_BOOT_ENTRY##*/}
        sweep_entry "$XEDA_BOOT_ENTRY" "${XEDA_BOOT_NAME#*.new.}"
    done
}

# ---- uv --------------------------------------------------------------------------------------

# Find the program that computes a SHA-256, before anything is downloaded. This is not in a command
# substitution, so that its error ends the script.
XEDA_BOOT_SHA_TOOL=
find_sha_tool() {
    if command -v sha256sum >/dev/null 2>&1; then
        XEDA_BOOT_SHA_TOOL=sha256sum
    elif command -v shasum >/dev/null 2>&1; then
        XEDA_BOOT_SHA_TOOL="shasum -a 256"
    else
        die no-sha256 "The remote host has neither sha256sum nor shasum, so it cannot check the download of uv."
    fi
}

sha256_of() {
    # shellcheck disable=SC2086
    $XEDA_BOOT_SHA_TOOL "$1" </dev/null | { read -r XEDA_BOOT_DIGEST XEDA_BOOT_REST; printf '%s' "$XEDA_BOOT_DIGEST"; }
}

download() {
    if command -v curl >/dev/null 2>&1; then
        run curl -fsSL -o "$2" "$1" || fail_step download-failed "curl could not download $1."
    elif command -v wget >/dev/null 2>&1; then
        run wget -q -O "$2" "$1" || fail_step download-failed "wget could not download $1."
    else
        die no-downloader "The remote host has neither curl nor wget. Install one of them to download uv."
    fi
}

# Print the path of the uv of the release $1 for the platform of the archive URL $2. Install it
# from the archive with the SHA-256 $3 if it is missing.
ensure_uv() {
    XEDA_BOOT_VERSION=$1
    XEDA_BOOT_URL=$2
    XEDA_BOOT_SHA=$3
    XEDA_BOOT_FILE=${XEDA_BOOT_URL##*/}
    XEDA_BOOT_TARGET=${XEDA_BOOT_FILE#uv-}
    XEDA_BOOT_TARGET=${XEDA_BOOT_TARGET%.tar.gz}
    XEDA_BOOT_UV_DIR=$XEDA_BOOT_V/uv/$XEDA_BOOT_VERSION-$XEDA_BOOT_TARGET
    XEDA_BOOT_UV=$XEDA_BOOT_UV_DIR/uv
    if [ -x "$XEDA_BOOT_UV" ]; then
        return 0
    fi
    acquire "uv-$XEDA_BOOT_VERSION-$XEDA_BOOT_TARGET"
    if [ ! -x "$XEDA_BOOT_UV" ]; then
        find_sha_tool
        say "STEP Installing uv $XEDA_BOOT_VERSION"
        make_work
        download "$XEDA_BOOT_URL" "$XEDA_BOOT_WORK/uv.tar.gz"
        XEDA_BOOT_DIGEST=$(sha256_of "$XEDA_BOOT_WORK/uv.tar.gz")
        if [ "$XEDA_BOOT_DIGEST" != "$XEDA_BOOT_SHA" ]; then
            die checksum-mismatch "The download of $XEDA_BOOT_URL has the SHA-256 $XEDA_BOOT_DIGEST, not the expected $XEDA_BOOT_SHA."
        fi
        mkdir "$XEDA_BOOT_WORK/unpacked"
        run tar -xzf "$XEDA_BOOT_WORK/uv.tar.gz" -C "$XEDA_BOOT_WORK/unpacked" \
            || fail_step download-failed "Cannot unpack the archive of uv."
        if [ ! -f "$XEDA_BOOT_WORK/unpacked/uv-$XEDA_BOOT_TARGET/uv" ]; then
            die download-failed "The archive of uv has no file uv-$XEDA_BOOT_TARGET/uv."
        fi
        chmod 755 "$XEDA_BOOT_WORK/unpacked/uv-$XEDA_BOOT_TARGET/uv"
        mkdir -p "$XEDA_BOOT_V/uv"
        XEDA_BOOT_STAGE=$XEDA_BOOT_UV_DIR.new.$XEDA_BOOT_HOST.$$
        mv "$XEDA_BOOT_WORK/unpacked/uv-$XEDA_BOOT_TARGET" "$XEDA_BOOT_STAGE"
        if ! run "$XEDA_BOOT_STAGE/uv" --version; then
            die download-failed "The downloaded uv does not run on this host ($XEDA_BOOT_TARGET)."
        fi
        rm -rf "$XEDA_BOOT_UV_DIR"
        mv "$XEDA_BOOT_STAGE" "$XEDA_BOOT_UV_DIR"
        XEDA_BOOT_STAGE=
    fi
    release "uv-$XEDA_BOOT_VERSION-$XEDA_BOOT_TARGET"
}

# ---- Commands --------------------------------------------------------------------------------

use_uv() {
    UV_CACHE_DIR=$XEDA_BOOT_V/cache/uv
    UV_PYTHON_INSTALL_DIR=$XEDA_BOOT_V/python
    UV_NO_PROGRESS=1
    UV_NO_CONFIG=1
    export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_NO_PROGRESS UV_NO_CONFIG
    # An active virtual environment of the login shell must not change which Python uv picks.
    unset VIRTUAL_ENV CONDA_PREFIX
    mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" || fail_step env-failed "Cannot create the directories of uv in $XEDA_BOOT_V."
}

# Set PYTHON_PATH to the Python for the request or path $1.
ensure_python() {
    case $1 in
        /*)
            if [ ! -x "$1" ]; then
                die python-failed "The Python '$1' of the host profile is not an executable file."
            fi
            XEDA_BOOT_PYTHON_PATH=$1
            return 0
            ;;
    esac
    XEDA_BOOT_REQUEST=$1
    if XEDA_BOOT_PYTHON_PATH=$("$XEDA_BOOT_UV" --no-config python find --managed-python "$XEDA_BOOT_REQUEST" 2>/dev/null </dev/null); then
        return 0
    fi
    acquire "python-$XEDA_BOOT_REQUEST"
    if ! XEDA_BOOT_PYTHON_PATH=$("$XEDA_BOOT_UV" --no-config python find --managed-python "$XEDA_BOOT_REQUEST" 2>/dev/null </dev/null); then
        say "STEP Installing Python ${XEDA_BOOT_REQUEST#cpython-}"
        run "$XEDA_BOOT_UV" --no-config python install "$XEDA_BOOT_REQUEST" \
            || fail_step python-failed "uv could not install the Python '$XEDA_BOOT_REQUEST'."
        XEDA_BOOT_PYTHON_PATH=$("$XEDA_BOOT_UV" --no-config python find --managed-python "$XEDA_BOOT_REQUEST" </dev/null) \
            || die python-failed "uv installed the Python '$XEDA_BOOT_REQUEST' but cannot find it."
    fi
    release "python-$XEDA_BOOT_REQUEST"
}

build_env() {
    XEDA_BOOT_KEY=$1
    XEDA_BOOT_ENV_DIR=$XEDA_BOOT_V/env/$XEDA_BOOT_KEY
    acquire "env-$XEDA_BOOT_KEY"
    if ! env_complete "$XEDA_BOOT_KEY"; then
        say "STEP Creating the environment"
        mkdir -p "$XEDA_BOOT_V/env"
        rm -rf "$XEDA_BOOT_ENV_DIR"
        XEDA_BOOT_ENV_BUILDING=$XEDA_BOOT_ENV_DIR
        run "$XEDA_BOOT_UV" --no-config venv --python "$XEDA_BOOT_PYTHON_PATH" "$XEDA_BOOT_ENV_DIR" \
            || fail_step env-failed "uv could not create the environment $XEDA_BOOT_ENV_DIR."
        run "$XEDA_BOOT_UV" --no-config pip install --python "$XEDA_BOOT_ENV_DIR/bin/python" \
            -r "$XEDA_BOOT_WORK/requirements.txt" -c "$XEDA_BOOT_WORK/constraints.txt" \
            || fail_step env-failed "uv could not install the requirements of xeda."
        "$XEDA_BOOT_UV" --no-config pip freeze --python "$XEDA_BOOT_ENV_DIR/bin/python" >"$XEDA_BOOT_ENV_DIR/xeda-lock.txt" </dev/null \
            || fail_step env-failed "uv could not list the installed packages."
        printf '%s\n' "$XEDA_BOOT_KEY" >"$XEDA_BOOT_ENV_DIR/.xeda-complete"
        XEDA_BOOT_ENV_BUILDING=
    fi
    release "env-$XEDA_BOOT_KEY"
}

command_status() {
    XEDA_BOOT_ENV_STATE=missing
    XEDA_BOOT_CODE_STATE=missing
    if env_complete "$1"; then
        XEDA_BOOT_ENV_STATE=present
    fi
    if code_complete "$2"; then
        XEDA_BOOT_CODE_STATE=present
    fi
    say "STATUS env=$XEDA_BOOT_ENV_STATE code=$XEDA_BOOT_CODE_STATE"
}

command_provision() {
    XEDA_BOOT_KEY=$1
    XEDA_BOOT_PYTHON=$2
    if env_complete "$XEDA_BOOT_KEY"; then
        return 0
    fi
    sweep_leftovers
    make_work
    : >"$XEDA_BOOT_WORK/requirements.txt"
    : >"$XEDA_BOOT_WORK/constraints.txt"
    while IFS= read -r XEDA_BOOT_LINE || [ -n "$XEDA_BOOT_LINE" ]; do
        case $XEDA_BOOT_LINE in
            'R '*) printf '%s\n' "${XEDA_BOOT_LINE#R }" >>"$XEDA_BOOT_WORK/requirements.txt" ;;
            'C '*) printf '%s\n' "${XEDA_BOOT_LINE#C }" >>"$XEDA_BOOT_WORK/constraints.txt" ;;
            '') ;;
            *) die bad-request "Not a requirement line: $XEDA_BOOT_LINE" ;;
        esac
    done
    if [ ! -s "$XEDA_BOOT_WORK/requirements.txt" ]; then
        die bad-request "The request has no requirements."
    fi
    use_uv
    ensure_uv "$3" "$4" "$5"
    ensure_python "$XEDA_BOOT_PYTHON"
    build_env "$XEDA_BOOT_KEY"
}

# Refuse a tar stream that has a name outside the destination, or a member that is not a regular
# file or a directory.
check_archive() {
    tar -tvf "$1" </dev/null >"$1.list" || die bad-request "The code archive cannot be read."
    tar -tf "$1" </dev/null >"$1.names" || die bad-request "The code archive cannot be read."
    while IFS= read -r XEDA_BOOT_LINE; do
        case $XEDA_BOOT_LINE in
            -* | d*) ;;
            *) die bad-request "The code archive has a member that is not a file or a directory." ;;
        esac
    done <"$1.list"
    while IFS= read -r XEDA_BOOT_NAME; do
        case /$XEDA_BOOT_NAME/ in
            */../* | //*) die bad-request "The code archive has the unsafe name '$XEDA_BOOT_NAME'." ;;
        esac
    done <"$1.names"
}

command_put_code() {
    XEDA_BOOT_KEY=$1
    if code_complete "$XEDA_BOOT_KEY"; then
        cat >/dev/null
        return 0
    fi
    sweep_leftovers
    make_work
    say "STEP Uploading xeda"
    cat >"$XEDA_BOOT_WORK/code.tar" || fail_step env-failed "Cannot write the code archive in $XEDA_BOOT_V."
    check_archive "$XEDA_BOOT_WORK/code.tar"
    mkdir -p "$XEDA_BOOT_V/code"
    XEDA_BOOT_STAGE=$XEDA_BOOT_V/code/.$XEDA_BOOT_KEY.$XEDA_BOOT_HOST.$$
    rm -rf "$XEDA_BOOT_STAGE"
    mkdir "$XEDA_BOOT_STAGE" || fail_step env-failed "Cannot create $XEDA_BOOT_STAGE."
    run tar -xf "$XEDA_BOOT_WORK/code.tar" -C "$XEDA_BOOT_STAGE" || fail_step env-failed "Cannot unpack the code archive."
    printf '%s\n' "$XEDA_BOOT_KEY" >"$XEDA_BOOT_STAGE/.xeda-complete"
    acquire "code-$XEDA_BOOT_KEY"
    if ! code_complete "$XEDA_BOOT_KEY"; then
        rm -rf "$XEDA_BOOT_V/code/$XEDA_BOOT_KEY"
        mv "$XEDA_BOOT_STAGE" "$XEDA_BOOT_V/code/$XEDA_BOOT_KEY"
        XEDA_BOOT_STAGE=
    fi
    release "code-$XEDA_BOOT_KEY"
}

command_agent() {
    XEDA_BOOT_DEPS=$1
    XEDA_BOOT_CODE=$2
    shift 2
    if ! env_complete "$XEDA_BOOT_DEPS" || ! code_complete "$XEDA_BOOT_CODE"; then
        die env-failed "The environment or the code is missing on the host. Run again."
    fi
    touch "$XEDA_BOOT_V/env/$XEDA_BOOT_DEPS/last-use" "$XEDA_BOOT_V/code/$XEDA_BOOT_CODE/last-use" 2>/dev/null || :
    PYTHONPATH=$XEDA_BOOT_V/code/$XEDA_BOOT_CODE
    VIRTUAL_ENV=$XEDA_BOOT_V/env/$XEDA_BOOT_DEPS
    export PYTHONPATH VIRTUAL_ENV
    unset PYTHONHOME
    trap - EXIT HUP INT PIPE TERM
    exec "$XEDA_BOOT_V/env/$XEDA_BOOT_DEPS/bin/python" -m xeda.remote.agent "$@"
}

# ---- Main ------------------------------------------------------------------------------------

case $XEDA_BOOT_COMMAND in
    status)
        [ $# -eq 2 ] || die bad-request "Usage: status <env-hash> <deps-key> <code-key>"
        check_key "dependency key" "$1"
        check_key "code key" "$2"
        command_status "$1" "$2"
        ;;
    provision)
        [ $# -eq 5 ] || die bad-request "Usage: provision <env-hash> <deps-key> <python> <uv-version> <uv-url> <uv-sha256>"
        check_key "dependency key" "$1"
        command_provision "$@"
        ;;
    put-code)
        [ $# -eq 1 ] || die bad-request "Usage: put-code <env-hash> <code-key>"
        check_key "code key" "$1"
        command_put_code "$1"
        ;;
    agent)
        [ $# -ge 2 ] || die bad-request "Usage: agent <env-hash> <deps-key> <code-key> <argument>..."
        check_key "dependency key" "$1"
        check_key "code key" "$2"
        command_agent "$@"
        ;;
    *)
        die bad-request "Unknown command '$XEDA_BOOT_COMMAND'."
        ;;
esac
