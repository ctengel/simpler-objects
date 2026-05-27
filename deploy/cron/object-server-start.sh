#!/usr/bin/env bash
# Wrapper for simpler-objects object server; designed for @reboot cron.
# Copy to ~/bin/simpler-objects-object-server-start, chmod +x, then edit
# MOUNT_WAIT_PATH and VENV below to match your installation.

# ---- operator-configurable variables ----------------------------------------
# Mount path that must be present before starting (parent of OBJECT_DIRECTORY).
# Edit to match your disk, e.g. /mnt/extusb-a or /mnt/data.
MOUNT_WAIT_PATH="/mnt/extusb-a"

# Python venv containing the simpler-objects package.
VENV="${HOME}/venv"

# Where to write logs and the PID file.
LOG_FILE="${HOME}/logs/simpler-objects-object-server.log"
PID_FILE="${HOME}/.run/simpler-objects-object-server.pid"
# -----------------------------------------------------------------------------

ENV_FILE="${HOME}/.config/simpler-objects/object-server.env"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"
exec >> "$LOG_FILE" 2>&1

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }

log "object-server-start: starting up"

# Source env file.
[ -f "$ENV_FILE" ] || { log "FATAL: env file not found: $ENV_FILE"; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-29171}"
WORKERS="${WORKERS:-1}"

[ -n "$OBJECT_DIRECTORY" ] || { log "FATAL: OBJECT_DIRECTORY not set in $ENV_FILE"; exit 1; }

# Wait for mount.
until mountpoint -q "$MOUNT_WAIT_PATH"; do
    log "Waiting for $MOUNT_WAIT_PATH to mount..."
    sleep 5
done
log "Mount $MOUNT_WAIT_PATH is ready."

# Scrub preflight — mirrors ExecStartPre in the systemd unit.
# On non-zero exit (orphan files or garbled checksums), refuse to start.
# Recover by running: python -m simpler_objects.scrub --delete-victims --repair-checksums $OBJECT_DIRECTORY
log "Running scrub preflight on $OBJECT_DIRECTORY"
if ! "$VENV/bin/python" -m simpler_objects.scrub "$OBJECT_DIRECTORY"; then
    log "FATAL: scrub found issues; refusing to start."
    log "       Run scrub with --delete-victims --repair-checksums to recover, then restart manually."
    exit 1
fi
log "Scrub clean."

# Restart loop — mirrors Restart=on-failure, RestartSec=5 in the systemd unit.
while true; do
    log "Starting uvicorn on ${HOST}:${PORT} (workers=${WORKERS})"
    "$VENV/bin/uvicorn" simpler_objects.object_server:app \
        --host "$HOST" --port "$PORT" --workers "$WORKERS" &
    echo $! > "$PID_FILE"
    wait $!
    rc=$?
    rm -f "$PID_FILE"
    if [ "$rc" -eq 0 ]; then
        log "uvicorn exited cleanly (rc=0); not restarting."
        exit 0
    fi
    log "uvicorn exited with rc=$rc; restarting in 5s"
    sleep 5
done
