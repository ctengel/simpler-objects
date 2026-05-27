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
