#!/usr/bin/env bash
# Container entrypoint for snapchat-saver.
#
# Runs as root (the image default) only long enough to:
#   * create a user matching PUID/PGID (default 568:568, the TrueNAS SCALE
#     "apps" user) and make DATA_DIR owned by it,
#   * remove stale X / Chromium lock files left behind by an unclean stop,
# then hands over to supervisord, which starts every program as that user.
# supervisord itself stays root so it can write to the container's
# stdout/stderr and restart programs; no workload process runs as root.
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*" >&2; }

DATA_DIR="${DATA_DIR:-/data}"
PUID="${PUID:-568}"
PGID="${PGID:-568}"
DISPLAY_NUM="${DISPLAY:-:99}"
DISPLAY_NUM="${DISPLAY_NUM#:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"
RUN_HOME="/home/snapchat-saver"

case "$PUID$PGID" in
    *[!0-9]*) log "PUID and PGID must be numeric (got PUID=$PUID PGID=$PGID)"; exit 1 ;;
esac

setup_user() {
    # Group: reuse an existing group with that GID, otherwise create one.
    local group user
    group="$(getent group "$PGID" | cut -d: -f1 || true)"
    if [ -z "$group" ]; then
        groupadd -g "$PGID" snapsaver
        group=snapsaver
    fi
    # User: reuse an existing account with that UID, otherwise create one.
    user="$(getent passwd "$PUID" | cut -d: -f1 || true)"
    if [ -z "$user" ]; then
        useradd -u "$PUID" -g "$PGID" -d "$RUN_HOME" -M -s /usr/sbin/nologin snapsaver
        user=snapsaver
    fi
    RUN_USER="$user"
    RUN_GROUP="$group"
}

if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" = "0" ]; then
        log "WARNING: PUID=0, all processes will run as root. This is not recommended."
        RUN_USER=root
        RUN_GROUP=root
    else
        setup_user
    fi
else
    # Started with `user:` / `--user`: no privilege changes are possible, so
    # everything simply runs as the current user. DATA_DIR must already be
    # writable by it.
    RUN_USER="$(id -un 2>/dev/null || id -u)"
    RUN_GROUP="$(id -gn 2>/dev/null || id -g)"
    RUN_HOME="${HOME:-/tmp/snapchat-saver-home}"
    PUID="$(id -u)"
    PGID="$(id -g)"
    log "Running without root; PUID/PGID are ignored (using $PUID:$PGID)."
fi

# --- data directory -------------------------------------------------------
mkdir -p "$DATA_DIR" \
         "$DATA_DIR/images" \
         "$DATA_DIR/camera" \
         "$DATA_DIR/debug" \
         "$DATA_DIR/chromium-profile"
mkdir -p "$RUN_HOME"

if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    chown "$PUID:$PGID" "$RUN_HOME"
    # Only walk the tree when something is not owned by the run user yet.
    if [ -n "$(find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit 2>/dev/null)" ]; then
        log "Setting ownership of $DATA_DIR to $PUID:$PGID"
        if ! chown -R "$PUID:$PGID" "$DATA_DIR"; then
            log "WARNING: could not chown $DATA_DIR (NFS/SMB share or ACL dataset?)."
            log "         Make sure uid $PUID / gid $PGID can write to it, or change PUID/PGID."
        fi
    fi
fi

# shellcheck disable=SC2016  # $1 is expanded by the inner sh, on purpose
write_test=(sh -c 'touch "$1/.write-test" && rm -f "$1/.write-test"' sh "$DATA_DIR")
if [ "$(id -u)" = "0" ]; then
    write_test=(setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "${write_test[@]}")
fi
if ! "${write_test[@]}" 2>/dev/null; then
    log "WARNING: $DATA_DIR is not writable by uid $PUID gid $PGID; the app will fail to save data."
fi

# --- stale locks from an unclean shutdown ----------------------------------
# Xvfb refuses to start while its lock file / socket exist.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
# Chromium refuses to open a profile that another (now dead) instance
# "holds". These are symlinks, possibly dangling, so use rm -f.
rm -f "$DATA_DIR/chromium-profile/SingletonLock" \
      "$DATA_DIR/chromium-profile/SingletonCookie" \
      "$DATA_DIR/chromium-profile/SingletonSocket"

export RUN_USER RUN_GROUP RUN_HOME DATA_DIR
export DISPLAY=":${DISPLAY_NUM}"
export SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x900x24}"

log "Starting as $RUN_USER ($PUID:$PGID), data in $DATA_DIR, display $DISPLAY ($SCREEN_GEOMETRY)"
exec "$@"
