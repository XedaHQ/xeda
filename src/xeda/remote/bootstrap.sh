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
HELD_LOCKS=
WORK=
STAGE=
ENV_BUILDING=
CHILD=

cleanup() {
    status=$?
    trap - EXIT
    if [ -n "$CHILD" ]; then
        kill "$CHILD" 2>/dev/null || :
    fi
    if [ -n "$ENV_BUILDING" ]; then
        rm -rf "$ENV_BUILDING"
    fi
    if [ -n "$STAGE" ]; then
        rm -rf "$STAGE"
    fi
    if [ -n "$WORK" ]; then
        rm -rf "$WORK"
    fi
    for held in $HELD_LOCKS; do
        rm -rf "$LOCKS/$held.lock"
    done
    exit "$status"
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
V=$(cd "$(dirname "$0")/.." && pwd)
LOCKS=$V/locks
# The environment file of the host profile comes first: its variables apply to everything below.
if [ $# -lt 2 ]; then
    die bad-request "Usage: boot.sh <command> <env-hash> ..."
fi
command=$1
env_hash=$2
shift 2
case $env_hash in
    '' | *[!0-9a-f]*) die bad-request "The environment hash '$env_hash' is not a key." ;;
esac
if [ ! -f "$V/bin/env-$env_hash.sh" ]; then
    die bad-request "The environment file env-$env_hash.sh is missing."
fi
# shellcheck source=/dev/null
. "$V/bin/env-$env_hash.sh"

HOST=$(uname -n)
#: Seconds between two checks of a lock. The tests set a shorter time in the `env` of a profile.
POLL=${XEDA_BOOT_POLL:-1}
#: Seconds after which a lock that another process holds is reported, not waited for.
LOCK_LIMIT=1800
#: Free space in KB below which a failed step is reported as a full disk.
LOW_DISK_KB=51200

# ---- Helpers ---------------------------------------------------------------------------------

free_kb() {
    df -Pk "$V" 2>/dev/null </dev/null | {
        read -r _header || :
        read -r _name _size _used available _rest || :
        printf '%s' "${available:-}"
    }
}

# Report a failed step with `code`, or, if the disk is nearly full, as a full disk.
fail_step() {
    code=$1
    message=$2
    free=$(free_kb)
    case $free in
        '' | *[!0-9]*) ;;
        *)
            if [ "$free" -lt "$LOW_DISK_KB" ]; then
                die no-space "The disk of $V has only $free KB free. $message"
            fi
            ;;
    esac
    die "$code" "$message"
}

# Run a tool with its output on the standard error and no standard input. The tool is a child
# process that a signal can stop: the shell runs the trap of a signal only after a foreground
# command ends, but `wait` ends at once.
run() {
    "$@" >&2 </dev/null &
    CHILD=$!
    run_status=0
    wait "$CHILD" || run_status=$?
    CHILD=
    return "$run_status"
}

check_key() {
    case $2 in
        '' | *[!0-9a-f]*) die bad-request "The $1 '$2' is not a key." ;;
    esac
}

# The files `.xeda-complete` hold the key of their directory. A directory that has no such
# file, or another one, is not complete: the process that made it did not finish.
env_complete() {
    [ -f "$V/env/$1/.xeda-complete" ] \
        && [ "$(cat "$V/env/$1/.xeda-complete")" = "$1" ] \
        && [ -x "$V/env/$1/bin/python" ]
}

code_complete() {
    [ -f "$V/code/$1/.xeda-complete" ] \
        && [ "$(cat "$V/code/$1/.xeda-complete")" = "$1" ] \
        && [ -d "$V/code/$1/xeda" ]
}

# Make the work directory of this process, once. It is on the file system of the layout, so that
# a rename from it into the layout is atomic.
make_work() {
    if [ -z "$WORK" ]; then
        mkdir -p "$V/tmp" || fail_step env-failed "Cannot create $V/tmp."
        WORK=$V/tmp/$$
        rm -rf "$WORK"
        mkdir "$WORK" || fail_step env-failed "Cannot create $WORK."
    fi
}

# ---- Locks -----------------------------------------------------------------------------------

# A lock is the directory `locks/<name>.lock` with the file `owner`: the host, the process and the
# time. A lock whose owner process is gone on this host is broken. The owner of a lock that is
# older than LOCK_LIMIT is reported, because this script cannot know whether a process on another
# host is alive.

process_alive() {
    kill -0 "$1" 2>/dev/null || [ -d "/proc/$1" ]
}

# Break the lock directory $1 if its owner is still the one in $2, $3 and $4. Only one process
# breaks a lock at a time, and it checks the owner again, so that no process breaks a new lock.
break_lock() {
    breaker=$1.break
    if mkdir "$breaker" 2>/dev/null; then
        if read -r again_host again_pid again_time _rest 2>/dev/null <"$1/owner" \
            && [ "$again_host $again_pid $again_time" = "$2 $3 $4" ]; then
            rm -rf "$1"
        fi
        rmdir "$breaker"
    fi
}

acquire() {
    name=$1
    lock=$LOCKS/$name.lock
    mkdir -p "$LOCKS" || fail_step env-failed "Cannot create $LOCKS."
    announced=
    unknown=0
    while :; do
        if mkdir "$lock" 2>/dev/null; then
            HELD_LOCKS="$HELD_LOCKS $name"
            printf '%s %s %s\n' "$HOST" "$$" "$(date +%s)" >"$lock/owner"
            return 0
        fi
        owner_host=
        owner_pid=
        owner_time=
        if [ -r "$lock/owner" ]; then
            read -r owner_host owner_pid owner_time _rest 2>/dev/null <"$lock/owner" || :
        fi
        case $owner_pid:$owner_time in
            *[!0-9:]* | :* | *:)
                # The process that made the lock did not write its owner file yet, or it
                # stopped before it did.
                unknown=$((unknown + 1))
                if [ "$unknown" -gt 60 ]; then
                    break_lock "$lock" "$owner_host" "$owner_pid" "$owner_time"
                    unknown=0
                fi
                ;;
            *)
                unknown=0
                if [ "$owner_host" = "$HOST" ] && ! process_alive "$owner_pid"; then
                    break_lock "$lock" "$owner_host" "$owner_pid" "$owner_time"
                    if [ ! -d "$lock" ]; then
                        continue
                    fi
                fi
                age=$(($(date +%s) - owner_time))
                if [ "$age" -gt "$LOCK_LIMIT" ]; then
                    die lock-timeout "The lock $lock has been held for $((age / 60)) minutes by process $owner_pid on $owner_host. If that process does not run, remove the directory."
                fi
                ;;
        esac
        if [ -z "$announced" ]; then
            say "STEP Waiting for another xeda process ($name)"
            announced=1
        fi
        sleep "$POLL"
    done
}

release() {
    rm -rf "$LOCKS/$1.lock"
    kept=
    for held in $HELD_LOCKS; do
        if [ "$held" != "$1" ]; then
            kept="$kept $held"
        fi
    done
    HELD_LOCKS=$kept
}

# ---- uv --------------------------------------------------------------------------------------

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" </dev/null | { read -r digest _rest; printf '%s' "$digest"; }
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" </dev/null | { read -r digest _rest; printf '%s' "$digest"; }
    else
        die env-failed "The remote host has neither sha256sum nor shasum, so it cannot check the download."
    fi
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
    version=$1
    url=$2
    sha=$3
    file=${url##*/}
    target=${file#uv-}
    target=${target%.tar.gz}
    uv_dir=$V/uv/$version-$target
    UV=$uv_dir/uv
    if [ -x "$UV" ]; then
        return 0
    fi
    acquire "uv-$version-$target"
    if [ ! -x "$UV" ]; then
        say "STEP Installing uv $version"
        make_work
        download "$url" "$WORK/uv.tar.gz"
        digest=$(sha256_of "$WORK/uv.tar.gz")
        if [ "$digest" != "$sha" ]; then
            die checksum-mismatch "The download of $url has the SHA-256 $digest, not the expected $sha."
        fi
        mkdir "$WORK/unpacked"
        run tar -xzf "$WORK/uv.tar.gz" -C "$WORK/unpacked" \
            || fail_step download-failed "Cannot unpack the archive of uv."
        if [ ! -f "$WORK/unpacked/uv-$target/uv" ]; then
            die download-failed "The archive of uv has no file uv-$target/uv."
        fi
        chmod 755 "$WORK/unpacked/uv-$target/uv"
        mkdir -p "$V/uv"
        STAGE=$uv_dir.new.$$
        mv "$WORK/unpacked/uv-$target" "$STAGE"
        if ! run "$STAGE/uv" --version; then
            die download-failed "The downloaded uv does not run on this host ($target)."
        fi
        rm -rf "$uv_dir"
        mv "$STAGE" "$uv_dir"
        STAGE=
    fi
    release "uv-$version-$target"
}

# ---- Commands --------------------------------------------------------------------------------

use_uv() {
    UV_CACHE_DIR=$V/cache/uv
    UV_PYTHON_INSTALL_DIR=$V/python
    UV_NO_PROGRESS=1
    export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_NO_PROGRESS
    mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" || fail_step env-failed "Cannot create the directories of uv in $V."
}

# Set PYTHON_PATH to the Python for the request or path $1.
ensure_python() {
    case $1 in
        /*)
            if [ ! -x "$1" ]; then
                die python-failed "The Python '$1' of the host profile is not an executable file."
            fi
            PYTHON_PATH=$1
            return 0
            ;;
    esac
    request=$1
    if PYTHON_PATH=$("$UV" --no-config python find --managed-python "$request" 2>/dev/null </dev/null); then
        return 0
    fi
    acquire "python-$request"
    if ! PYTHON_PATH=$("$UV" --no-config python find --managed-python "$request" 2>/dev/null </dev/null); then
        say "STEP Installing Python ${request#cpython-}"
        run "$UV" --no-config python install "$request" \
            || fail_step python-failed "uv could not install the Python '$request'."
        PYTHON_PATH=$("$UV" --no-config python find --managed-python "$request" </dev/null) \
            || die python-failed "uv installed the Python '$request' but cannot find it."
    fi
    release "python-$request"
}

build_env() {
    key=$1
    env_dir=$V/env/$key
    acquire "env-$key"
    if ! env_complete "$key"; then
        say "STEP Creating the environment"
        mkdir -p "$V/env"
        rm -rf "$env_dir"
        ENV_BUILDING=$env_dir
        run "$UV" --no-config venv --python "$PYTHON_PATH" "$env_dir" \
            || fail_step env-failed "uv could not create the environment $env_dir."
        run "$UV" --no-config pip install --python "$env_dir/bin/python" \
            -r "$WORK/requirements.txt" -c "$WORK/constraints.txt" \
            || fail_step env-failed "uv could not install the requirements of xeda."
        "$UV" --no-config pip freeze --python "$env_dir/bin/python" >"$env_dir/xeda-lock.txt" </dev/null \
            || fail_step env-failed "uv could not list the installed packages."
        printf '%s\n' "$key" >"$env_dir/.xeda-complete"
        ENV_BUILDING=
    fi
    release "env-$key"
}

command_status() {
    env_state=missing
    code_state=missing
    if env_complete "$1"; then
        env_state=present
    fi
    if code_complete "$2"; then
        code_state=present
    fi
    say "STATUS env=$env_state code=$code_state"
}

command_provision() {
    key=$1
    python=$2
    if env_complete "$key"; then
        return 0
    fi
    make_work
    : >"$WORK/requirements.txt"
    : >"$WORK/constraints.txt"
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in
            'R '*) printf '%s\n' "${line#R }" >>"$WORK/requirements.txt" ;;
            'C '*) printf '%s\n' "${line#C }" >>"$WORK/constraints.txt" ;;
            '') ;;
            *) die bad-request "Not a requirement line: $line" ;;
        esac
    done
    if [ ! -s "$WORK/requirements.txt" ]; then
        die bad-request "The request has no requirements."
    fi
    use_uv
    ensure_uv "$3" "$4" "$5"
    ensure_python "$python"
    build_env "$key"
}

# Refuse a tar stream that has a name outside the destination, or a member that is not a regular
# file or a directory.
check_archive() {
    tar -tvf "$1" </dev/null >"$1.list" || die bad-request "The code archive cannot be read."
    tar -tf "$1" </dev/null >"$1.names" || die bad-request "The code archive cannot be read."
    while IFS= read -r line; do
        case $line in
            -* | d*) ;;
            *) die bad-request "The code archive has a member that is not a file or a directory." ;;
        esac
    done <"$1.list"
    while IFS= read -r name; do
        case /$name/ in
            */../* | //*) die bad-request "The code archive has the unsafe name '$name'." ;;
        esac
    done <"$1.names"
}

command_put_code() {
    key=$1
    if code_complete "$key"; then
        cat >/dev/null
        return 0
    fi
    make_work
    say "STEP Uploading xeda"
    cat >"$WORK/code.tar" || fail_step env-failed "Cannot write the code archive in $V."
    check_archive "$WORK/code.tar"
    mkdir -p "$V/code"
    STAGE=$V/code/.$key.$$
    rm -rf "$STAGE"
    mkdir "$STAGE" || fail_step env-failed "Cannot create $STAGE."
    run tar -xf "$WORK/code.tar" -C "$STAGE" || fail_step env-failed "Cannot unpack the code archive."
    printf '%s\n' "$key" >"$STAGE/.xeda-complete"
    acquire "code-$key"
    if ! code_complete "$key"; then
        rm -rf "$V/code/$key"
        mv "$STAGE" "$V/code/$key"
        STAGE=
    fi
    release "code-$key"
}

command_agent() {
    deps=$1
    code=$2
    shift 2
    if ! env_complete "$deps" || ! code_complete "$code"; then
        die env-failed "The environment or the code is missing on the host. Run again."
    fi
    touch "$V/env/$deps/last-use" "$V/code/$code/last-use" 2>/dev/null || :
    PYTHONPATH=$V/code/$code
    export PYTHONPATH
    trap - EXIT HUP INT PIPE TERM
    exec "$V/env/$deps/bin/python" -m xeda.remote.agent "$@"
}

# ---- Main ------------------------------------------------------------------------------------

case $command in
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
        die bad-request "Unknown command '$command'."
        ;;
esac
