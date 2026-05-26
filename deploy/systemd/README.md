# systemd deployment

Production deployment of `simpler-objects` via systemd. The repo ships:

- `simpler-objects-object-server@.service` — template unit, one instance per attached disk
- `simpler-objects-locator.service` — single-instance locator
- `env/object-server.env.example`, `env/locator.env.example` — per-host configuration

> Driving a fleet of Pis? An Ansible playbook that wraps this entire walkthrough
> — install, per-instance config, future upgrades — lives at
> [`../ansible/`](../ansible/). It uses the same unit files; the manual
> instructions below are the source of truth for what it does.

## Tested on

- Fedora
- Raspberry Pi OS (trixie; or bookworm with Python 3.12 manually installed)

## Install the package

The unit files expect the package installed into a venv at `/opt/simpler-objects/venv`. If you put it elsewhere, override the `ExecStart=` path with a drop-in (`systemctl edit <unit>`).

### Fedora

```
sudo dnf install -y python3 python3-venv git
sudo mkdir -p /opt/simpler-objects
sudo python3 -m venv /opt/simpler-objects/venv
sudo /opt/simpler-objects/venv/bin/pip install \
    git+https://github.com/ctengel/simpler-objects@v0.4.0
```

### Raspberry Pi OS

```
sudo apt update
sudo apt install -y python3 python3-venv git
sudo mkdir -p /opt/simpler-objects
sudo python3 -m venv /opt/simpler-objects/venv
sudo /opt/simpler-objects/venv/bin/pip install \
    git+https://github.com/ctengel/simpler-objects@v0.4.0
```

No `libcurl` headers are required for the server install — `pycurl` is only in the optional `[client]` extra.

## Create the service user

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin simpler-objects
```

## Object server (Raspberry Pi storage node)

### Mount-path convention

Each instance's storage **must** be mounted at `/srv/simpler-objects/<instance>`. The unit's `RequiresMountsFor=/srv/simpler-objects/%i` encodes this, so the service won't start until the disk is mounted — which is the whole point on a Pi with an external USB drive that may not be ready when networking comes up.

If your mount lives elsewhere, use a per-instance drop-in. See "Non-standard mount paths" below for the full recipe.

### Wire up an instance

For each attached disk (e.g. `disk1`, `disk2`):

1. Mount it at `/srv/simpler-objects/disk1` via `/etc/fstab` (this is the part you get to design — make sure the device is identified by UUID or label, not by `/dev/sda1`).
2. `sudo chown -R simpler-objects:simpler-objects /srv/simpler-objects/disk1`
3. Copy the example env file and edit it:

   ```
   sudo install -d -m 0750 -o root -g simpler-objects /etc/simpler-objects
   sudo install -m 0640 -o root -g simpler-objects \
       deploy/systemd/env/object-server.env.example \
       /etc/simpler-objects/object-server-disk1.env
   sudoedit /etc/simpler-objects/object-server-disk1.env
   ```

   Each instance gets its own env file. The `<instance>` token in the filename must match the `@<instance>` in the systemctl command.
4. Install the unit and start the service:

   ```
   sudo install -m 0644 deploy/systemd/simpler-objects-object-server@.service \
       /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now simpler-objects-object-server@disk1
   sudo systemctl status simpler-objects-object-server@disk1
   ```

5. Repeat steps 1–3 for `disk2`, `disk3`, etc; the same unit template handles them all.

### Non-standard mount paths

If you can't (or don't want to) mount storage at `/srv/simpler-objects/<instance>` — for example, you already mount your external USB drive at `/mnt/extusb` and want to keep object data at `/mnt/extusb/simplerobjectsdata` — override the three coupled settings with a per-instance drop-in:

```
sudo systemctl edit simpler-objects-object-server@disk1
```

```ini
[Unit]
RequiresMountsFor=
RequiresMountsFor=/mnt/extusb/simplerobjectsdata

[Service]
ReadWritePaths=
ReadWritePaths=/mnt/extusb/simplerobjectsdata
```

Then in `/etc/simpler-objects/object-server-disk1.env`:

```
OBJECT_DIRECTORY=/mnt/extusb/simplerobjectsdata
```

The empty `RequiresMountsFor=` / `ReadWritePaths=` lines are required: without them systemd *appends* the drop-in values to the originals, leaving you with both `/srv/simpler-objects/disk1` (from the base unit) and `/mnt/extusb/simplerobjectsdata` (from the drop-in) — half-broken.

`RequiresMountsFor=` accepts paths *inside* a mount; systemd walks up and finds the deepest covering mount unit, so passing the data directory works as well as passing the mountpoint itself.

After editing, reload and restart:

```
sudo systemctl daemon-reload
sudo systemctl restart simpler-objects-object-server@disk1
```

### Worker count

Default is 1. To raise it, set `WORKERS=` in the env file (e.g. `WORKERS=4` on a busy node). `object_server.py`'s kernel-level flock + `O_CREAT|O_EXCL` + `O_APPEND` semantics make multi-worker safe across processes sharing the same `OBJECT_DIRECTORY`.

## Locator

Single instance — no template:

```
sudo install -d -m 0750 -o root -g simpler-objects /etc/simpler-objects
sudo install -m 0640 -o root -g simpler-objects \
    deploy/systemd/env/locator.env.example \
    /etc/simpler-objects/locator.env
sudoedit /etc/simpler-objects/locator.env

sudo install -m 0644 deploy/systemd/simpler-objects-locator.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simpler-objects-locator
```

Set `OBJECT_SERVERS=` in the env file to the comma-separated list of object servers (trailing slashes required).

The shipped unit runs as the `simpler-objects` user for install-time consistency with the object server. The locator is stateless and could equally run with `DynamicUser=yes`; flip it via a drop-in if you prefer.

## Scheduled replication

`simpler-objects-async-replicate.service` and its paired timer run `async_replicate` periodically across all configured buckets. Bucket names and replica counts come from `/etc/simpler-objects/async-replicate.env`.

### Install

```
sudo install -d -m 0750 -o root -g simpler-objects /etc/simpler-objects
sudo install -m 0640 -o root -g simpler-objects \
    deploy/systemd/env/async-replicate.env.example \
    /etc/simpler-objects/async-replicate.env
sudoedit /etc/simpler-objects/async-replicate.env   # set LOCATOR_URL, BUCKETS, REPLICAS

sudo install -m 0644 deploy/systemd/simpler-objects-async-replicate.service \
    deploy/systemd/simpler-objects-async-replicate.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simpler-objects-async-replicate.timer
```

### Per-bucket replica overrides

In `/etc/simpler-objects/async-replicate.env`, add a `REPLICAS_<UPPERCASE_BUCKET>=N` line for each bucket that differs from the default `REPLICAS`:

```
BUCKETS=photos backups archive
REPLICAS=2
REPLICAS_BACKUPS=3
REPLICAS_ARCHIVE=1
```

### Run once manually

```
sudo systemctl start simpler-objects-async-replicate.service
journalctl -u simpler-objects-async-replicate.service -n 50
```

`simpler-objects-async-replicate` exits non-zero if any object in any bucket couldn't be replicated (out of space, locator unreachable, source unavailable). systemd records the failure but does not retry until the next timer firing — that's the intended behaviour. If you want the locator on the same host as the replicator to come up first, the unit file comments show the drop-in.

`Type=oneshot` prevents concurrent runs: if the hourly timer fires while a previous run is still active, systemd queues the new start and runs it back-to-back after the first finishes. For this idempotent job that is harmless — the second run finds replicas already satisfied and exits quickly.

For cron-based scheduling instead, see [`../cron/README.md`](../cron/README.md).

## Operations

- Logs: `journalctl -u simpler-objects-object-server@disk1 -f`, `journalctl -u simpler-objects-locator -f`
- Reload after editing an env file: `sudo systemctl restart simpler-objects-object-server@disk1`
- List all running instances: `systemctl list-units 'simpler-objects-*'`
- Health check: `curl http://<host>:29164/health` (locator) or `curl http://<host>:29171/health` (object server)
