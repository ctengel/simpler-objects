# cron-based replication scheduling

Cron alternative to the systemd timer in `deploy/systemd/` for `simpler_objects.async_replicate`. Pick one — running both schedules the same work twice.

## Files

- `async-replicate.cron.example` — drop straight into `/etc/cron.d/` after editing bucket names, replica counts, and minute fields.
- `async-replicate.sh.example` — optional wrapper for installs that prefer env-var-driven cron entries (e.g. with `/etc/default/simpler-objects` defining `LOCATOR_URL`).

## Install (Fedora and Raspberry Pi OS)

Assumes the package is installed at `/opt/simpler-objects/venv` and the `simpler-objects` system user already exists (see `../systemd/README.md`).

```
sudo install -m 0644 deploy/cron/async-replicate.cron.example \
    /etc/cron.d/simpler-objects-async-replicate
sudoedit /etc/cron.d/simpler-objects-async-replicate
```

Edit the bucket names, replica counts, and minute fields. **Stagger the minute fields** across buckets so the replicators don't all kick off at the same instant.

To use the env-var wrapper instead:

```
sudo install -m 0755 deploy/cron/async-replicate.sh.example \
    /usr/local/sbin/simpler-objects-async-replicate.sh
sudo install -d -m 0755 /etc/default
echo 'LOCATOR_URL=http://localhost:29164/' | sudo tee /etc/default/simpler-objects
# Cron lines then become:
#   17 * * * * simpler-objects /usr/local/sbin/simpler-objects-async-replicate.sh photos
```

(Note: cron does not source `/etc/default/*` itself. Either reference variables via `BashEnv`-style cron syntax that supports it, or have the wrapper `. /etc/default/simpler-objects` before exec'ing the binary. Adjust to your distro's conventions.)

## Operational notes

- Logs go to syslog/journal under the cron daemon; on systemd-cron systems, `journalctl _COMM=cron` (or `journalctl -t CRON`) surfaces them. Failed runs trigger the usual cron mail to the cron user's mailbox.
- `simpler-objects-async-replicate` exits non-zero on any per-object warning (out of space, locator unreachable, source unavailable). Cron treats that as a failed job and mails it; the next firing retries naturally.
- The locator does **not** need to be on the same host. Set `LOCATOR_URL` to wherever it lives.
- If you want the locator to be running before replication starts (and they're on the same box), use the systemd timer instead — cron has no service-ordering primitive.
