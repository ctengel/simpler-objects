# systemd deployment

Production deployment of `simpler-objects` via systemd user units. Services
run entirely under the service account — no root involvement after the
one-time prerequisites are done.

The repo ships:

- `simpler-objects-object-server.service` — single-instance object-server unit (one per host; one disk per host in the user-unit layout)
- `simpler-objects-locator.service` — single-instance locator
- `simpler-objects-async-replicate.service` / `.timer` — replication job and timer
- `env/object-server.env.example`, `env/locator.env.example` — per-host configuration

> Driving a fleet of Pis? An Ansible playbook that wraps this entire walkthrough
> — install, per-instance config, future upgrades — lives at
> [`../ansible/`](../ansible/). It uses the same unit files; the manual
> instructions below are the source of truth for what it does.

## Tested on

- Fedora
- Raspberry Pi OS (trixie; or bookworm with Python 3.12 manually installed)

---

## Prerequisites (run once as root)

These steps require root. Everything after this section runs as the service user.

### 1. Install OS packages

**Fedora:**
```
sudo dnf install -y python3 python3-venv python3-packaging
```

**Raspberry Pi OS / Debian:**
```
sudo apt update && sudo apt install -y python3 python3-venv python3-packaging
```

`python3-packaging` is required by Ansible's `pip` module for version
comparison — install it on the system Python even though the app itself runs
in a venv.

The package is installed from a release tarball (below), so `git` is not
required. No `libcurl` headers are required for the server install either —
`pycurl` is only in the optional `[client]` extra.

### 2. Create the service user

```
sudo useradd simpler-objects
```

systemd user units need a home directory (the `%h` specifier in the unit files
expands to it). On Fedora and Raspberry Pi OS this is created automatically
by `useradd` via `CREATE_HOME yes` in `/etc/login.defs`; pass `--create-home`
explicitly if your distro defaults to no.

### 3. Mount the storage disk and hand off ownership

Mount the disk via `/etc/fstab` (use a UUID or label, not `/dev/sdX`). Then
create and chown the data subdirectory to the service user:

```
sudo mkdir -p /mnt/extusb-a/simpler-objects/data
sudo chown simpler-objects:simpler-objects /mnt/extusb-a/simpler-objects/data
```

The service user only needs ownership of the data subdirectory — not the
mountpoint itself.

### 4. Enable linger

Linger allows the service user's systemd session — and therefore its units —
to run without an active login session:

```
sudo loginctl enable-linger simpler-objects
```

---

## Install the package

Switch to the service user for all remaining steps:

```
sudo -u simpler-objects -s
```

Create a venv in the service user's home directory and install the package:

```
python3 -m venv ~/venv
~/venv/bin/pip install \
    https://github.com/ctengel/simpler-objects/archive/refs/tags/v0.4.4.tar.gz
```

(This is the gitless install the Ansible playbook also uses. To upgrade later,
re-run with a newer tag.)

---

## Object server (Raspberry Pi storage node)

### Wire up the object server

1. Copy and edit the env file:

   ```
   mkdir -p ~/.config/simpler-objects
   cp deploy/systemd/env/object-server.env.example \
       ~/.config/simpler-objects/object-server.env
   $EDITOR ~/.config/simpler-objects/object-server.env
   ```

   Set `OBJECT_DIRECTORY` to the data path chowned in the prerequisites (e.g.
   `/mnt/extusb-a/simpler-objects/data`).

2. Install the unit and create the `RequiresMountsFor=` drop-in:

   ```
   mkdir -p ~/.config/systemd/user
   cp deploy/systemd/simpler-objects-object-server.service ~/.config/systemd/user/

   # Drop-in so systemd waits for the disk before starting the service
   mkdir -p ~/.config/systemd/user/simpler-objects-object-server.service.d
   cat > ~/.config/systemd/user/simpler-objects-object-server.service.d/mount.conf <<'EOF'
   [Unit]
   RequiresMountsFor=/mnt/extusb-a/simpler-objects/data
   EOF

   systemctl --user daemon-reload
   systemctl --user enable --now simpler-objects-object-server
   systemctl --user status simpler-objects-object-server
   ```

### Worker count

Default is 1. To raise it, set `WORKERS=` in the env file. The kernel-level
`flock` + `O_CREAT|O_EXCL` + `O_APPEND` semantics make multi-worker safe
across processes sharing the same `OBJECT_DIRECTORY`.

---

## Locator

```
mkdir -p ~/.config/simpler-objects ~/.config/systemd/user
cp deploy/systemd/env/locator.env.example ~/.config/simpler-objects/locator.env
$EDITOR ~/.config/simpler-objects/locator.env

cp deploy/systemd/simpler-objects-locator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now simpler-objects-locator
```

Set `OBJECT_SERVERS=` in the env file to the comma-separated list of object
server URLs (trailing slashes required).

---

## Scheduled replication

```
mkdir -p ~/.config/simpler-objects ~/.config/systemd/user
cp deploy/systemd/env/async-replicate.env.example \
    ~/.config/simpler-objects/async-replicate.env
$EDITOR ~/.config/simpler-objects/async-replicate.env   # set LOCATOR_URL, BUCKETS, REPLICAS

cp deploy/systemd/simpler-objects-async-replicate.service \
   deploy/systemd/simpler-objects-async-replicate.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now simpler-objects-async-replicate.timer
```

### Per-bucket replica overrides

In `~/.config/simpler-objects/async-replicate.env`, add a
`REPLICAS_<UPPERCASE_BUCKET>=N` line for each bucket that differs from the
default `REPLICAS`. Dashes in bucket names become underscores in the env var
name (e.g. bucket `my-backups` → `REPLICAS_MY_BACKUPS`):

```
BUCKETS=photos backups archive my-backups
REPLICAS=2
REPLICAS_BACKUPS=3
REPLICAS_ARCHIVE=1
REPLICAS_MY_BACKUPS=5
```

### Run once manually

```
systemctl --user start simpler-objects-async-replicate.service
journalctl --user -u simpler-objects-async-replicate.service -n 50
```

`Type=oneshot` prevents concurrent runs: if the hourly timer fires while a
previous run is still active, systemd queues the new start and runs it
back-to-back after the first finishes.

For cron-based scheduling instead, see [`../cron/README.md`](../cron/README.md).

---

## Post-crash scrub preflight

Every start of `simpler-objects-object-server` runs
`simpler_objects.scrub` in dry-run mode against `OBJECT_DIRECTORY` first
(via `ExecStartPre=`). If a previous hard crash (SIGKILL / power loss) left an
orphan partial file or a garbled `<bucket>.sha256` line, scrub exits non-zero
and the unit refuses to start.

On a clean directory this is near-instant. The cost scales with file count.

### Recovering from a scrub failure

`systemctl --user status` shows the unit in `failed` state. The scrub output
is in the journal:

```
journalctl --user -u simpler-objects-object-server -n 50
```

Inspect the `crash-victim:`, `stale-entry:`, and `garbled-line:` lines, then
run scrub with the cleanup flags:

```
~/venv/bin/python -m simpler_objects.scrub \
    --delete-victims --repair-checksums /mnt/extusb-a/simpler-objects/data
systemctl --user reset-failed simpler-objects-object-server.service
systemctl --user start simpler-objects-object-server.service
```

`reset-failed` is required because systemd's start-limit kicks in after
repeated `ExecStartPre` failures.

---

## Operations

```
# Logs
journalctl --user -u simpler-objects-object-server -f
journalctl --user -u simpler-objects-locator -f

# Restart after editing an env file
systemctl --user restart simpler-objects-object-server

# List running simpler-objects units
systemctl --user list-units 'simpler-objects-*'

# Health check
curl http://<host>:29164/health        # locator
curl http://<host>:29171/health        # object server
```
