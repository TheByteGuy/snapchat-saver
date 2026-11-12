#!/bin/sh
# Wait (up to WAIT_FOR_X_TIMEOUT seconds, default 30) until the X server on
# $DISPLAY accepts connections, then exec the given command. Used so the
# window manager and x11vnc do not crash-loop while Xvfb is still starting.
set -eu

display="${DISPLAY:-:99}"
num="${display#:}"
num="${num%%.*}"
socket="/tmp/.X11-unix/X${num}"
timeout="${WAIT_FOR_X_TIMEOUT:-30}"

i=0
while [ ! -S "$socket" ]; do
    i=$((i + 1))
    if [ "$i" -gt $((timeout * 5)) ]; then
        echo "[wait-for-x] X server $display not ready after ${timeout}s, starting anyway" >&2
        break
    fi
    sleep 0.2
done

exec "$@"
