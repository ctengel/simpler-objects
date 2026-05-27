# cron-based replication scheduling

Cron alternative to the systemd timer in `deploy/systemd/` for `simpler_objects.async_replicate`. Pick one — running both schedules the same work twice.

## Files

- `async-replicate.cron.example` — drop into `/etc/cron.d/` after editing the bucket list, replica counts, and minute field.

## Install (Fedora and Raspberry Pi OS)

Assumes the package is installed at `/opt/simpler-objects/venv` and the `simpler-objects` system user already exists (see `../systemd/README.md`).

```
sudo install -m 0644 deploy/cron/async-replicate.cron.example \
    /etc/cron.d/simpler-objects-async-replicate
sudoedit /etc/cron.d/simpler-objects-async-replicate
```

Edit `BUCKETS`, `REPLICAS`, any `REPLICAS_<BUCKET>` overrides, and the minute field.

## Operational notes

- Logs go to syslog/journal under the cron daemon; on systemd-cron systems, `journalctl _COMM=cron` (or `journalctl -t CRON`) surfaces them. Failed runs trigger the usual cron mail to the cron user's mailbox.
- `simpler-objects-async-replicate` exits non-zero on any per-object warning (out of space, locator unreachable, source unavailable). Cron treats that as a failed job and mails it; the next firing retries naturally.
- The locator does **not** need to be on the same host. Set `LOCATOR_URL` in the cron line.
- If you want the locator to be running before replication starts (and they're on the same box), use the systemd timer instead — cron has no service-ordering primitive.

---

# @reboot startup (object server and locator)

Cron alternative to the systemd user units in `deploy/systemd/` for the object server and locator. Pick one per service — running both the systemd unit and the @reboot cron entry starts duplicate processes.

## Files

- `object-server-start.sh` — wrapper for the object server
- `locator-start.sh` — wrapper for the locator
- `simpler-objects-reboot.cron.example` — `/etc/cron.d/` snippet to install

## What the wrappers do

**`object-server-start.sh`**:
1. Sources `~/.config/simpler-objects/object-server.env`
2. Polls `mountpoint -q $MOUNT_WAIT_PATH` every 5 seconds until the disk is mounted
3. Runs `python -m simpler_objects.scrub $OBJECT_DIRECTORY` (dry-run); exits non-zero → logs the error and exits *without* starting uvicorn (same gate as `ExecStartPre=` in the systemd unit)
4. Starts uvicorn, writes its PID to `~/.run/simpler-objects-object-server.pid`
5. On non-zero exit waits 5 seconds and restarts (mirrors `Restart=on-failure, RestartSec=5`); on clean exit 0 stops

**`locator-start.sh`**: same restart loop, no mount-wait or scrub step.

## Install (manual)

```
# Copy and make executable
install -m 0755 deploy/cron/locator-start.sh \
    /home/simpler-objects/bin/simpler-objects-locator-start
install -m 0755 deploy/cron/object-server-start.sh \
    /home/simpler-objects/bin/simpler-objects-object-server-start

# Edit MOUNT_WAIT_PATH (and VENV if needed) in the object-server script
$EDITOR /home/simpler-objects/bin/simpler-objects-object-server-start

# Install cron file
sudo install -m 0644 deploy/cron/simpler-objects-reboot.cron.example \
    /etc/cron.d/simpler-objects-reboot
```

## Operations

```bash
# Logs
tail -f ~/logs/simpler-objects-locator.log
tail -f ~/logs/simpler-objects-object-server.log

# Check running
kill -0 "$(cat ~/.run/simpler-objects-locator.pid)"           # exit 0 → running
kill -0 "$(cat ~/.run/simpler-objects-object-server.pid)"

# Stop
kill "$(cat ~/.run/simpler-objects-locator.pid)"
kill "$(cat ~/.run/simpler-objects-object-server.pid)"

# Recover after a scrub failure (wrapper will have exited without starting uvicorn)
~/venv/bin/python -m simpler_objects.scrub \
    --delete-victims --repair-checksums /mnt/extusb-a/simpler-objects/data
# Then start manually or reboot
~/bin/simpler-objects-object-server-start &
```

## Differences vs. systemd units

| Feature | systemd unit | @reboot cron wrapper |
|---|---|---|
| Mount ordering | `RequiresMountsFor=` | `mountpoint` poll loop |
| Scrub preflight | `ExecStartPre=` | shell `if !` guard |
| Restart on failure | `Restart=on-failure` | `while` loop |
| Restart delay | `RestartSec=5s` | `sleep 5` |
| Logs | journald | `~/logs/*.log` |
| PID tracking | systemd | `~/.run/*.pid` |
| Sandboxing | `NoNewPrivileges=`, `PrivateTmp=`, etc. | none |
| Stop | `systemctl --user stop` | `kill $(cat ~/.run/*.pid)` |
