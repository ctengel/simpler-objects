# Ansible deployment

Ansible wrapper around the units in [`../systemd/`](../systemd/). Drives the
full simpler-objects stack — object servers, locator, and the async-replicate
timer — onto a fleet of Raspberry Pis (or any host), with version upgrades
reduced to a one-line YAML edit.

The playbook connects as the service user and needs **no root or sudo**. All
files land in that user's home directory. See "Prerequisites" for the one-time
root steps that must be done before running the playbook.

## Prerequisites (run once as root on each target host)

These steps require root. They only need to be done once.

```bash
# 1. Install OS packages (Debian/Raspberry Pi OS)
apt update && apt install -y python3 python3-venv

# 2. Create the service user. Home dir is required (systemd user units expand
#    %h to it); on Fedora and Raspberry Pi OS, useradd creates it by default.
#    Add --create-home explicitly if your distro defaults to CREATE_HOME no.
useradd simpler-objects

# 3. (Storage nodes) Mount disks via /etc/fstab, then chown the data subdir
mkdir -p /mnt/extusb-a/simpler-objects/data
chown simpler-objects:simpler-objects /mnt/extusb-a/simpler-objects/data

# 4. Enable linger so user units survive without a login session
loginctl enable-linger simpler-objects
```

## What this assumes about the target hosts

- Python 3.12+ and `python3-venv` installed (the package installs from a
  release tarball, so `git` is not required)
- The service user exists with a home directory and linger enabled (see Prerequisites)
- SSH access as the service user (e.g. deploy your SSH key to
  `/home/simpler-objects/.ssh/authorized_keys`)
- Storage disks are already partitioned, formatted, and mounted (via
  `/etc/fstab`). The data subdirectory must be owned by the service user
- The mount path can be anywhere — the role generates a per-instance systemd
  drop-in that points `RequiresMountsFor=` at whatever path you put in
  `object_directory`

## What this manages on the target hosts

- `~/venv` and the pip-installed package at a pinned git tag
- `~/.config/simpler-objects/*.env` per-service config files (mode 0600)
- The systemd user units under `~/.config/systemd/user/`
- A `RequiresMountsFor=` drop-in under
  `~/.config/systemd/user/simpler-objects-object-server.service.d/`
- Enabling and starting the units via `systemctl --user`

## Requirements (on the control node — your laptop)

- `ansible-core` ≥ 2.15
- SSH access to the target hosts as the service user

## Quickstart

```bash
cd deploy/ansible
cp inventory/hosts.example.yml inventory/hosts.yml
$EDITOR inventory/hosts.yml                              # set hosts, disks, ports

ansible-playbook --syntax-check site.yml                 # sanity check
ansible-playbook -i inventory/hosts.yml site.yml --check --diff   # dry run
ansible-playbook -i inventory/hosts.yml site.yml         # do it
```

The inventory `ansible_user` should be the service user (e.g.
`simpler-objects`). You can set it globally:

```yaml
all:
  vars:
    ansible_user: simpler-objects
    simpler_objects_version: v0.4.4
```

After a real run:

```bash
ssh -l simpler-objects pi-storage-1.lan \
    systemctl --user is-active simpler-objects-object-server
curl http://pi-storage-1.lan:29171/health
curl http://pi-coord.lan:29164/health
ssh -l simpler-objects pi-coord.lan \
    systemctl --user list-timers simpler-objects-async-replicate.timer
```

## Inventory shape

See [`inventory/hosts.example.yml`](inventory/hosts.example.yml) for an
annotated example with two storage Pis and a coordinator Pi. The knobs
the playbook exposes:

| Variable                    | Where      | Effect                                   |
|-----------------------------|------------|------------------------------------------|
| `simpler_objects_version`   | `all.vars` | Git tag to pip-install. Bump to upgrade. |
| `object_directory`          | per host   | Path to the bucket parent directory (required for object servers) |
| `object_server_read_only`   | per host   | `true` → RO mirror; default `false`      |
| `object_server_port`        | per host   | uvicorn listen port; default `29171`     |
| `object_server_workers`     | per host   | uvicorn worker count; default `1`        |

## Partial deployment — managing one service only

You don't have to use Ansible for all three services. The three plays in
`site.yml` each target a single inventory group (and each installs the venv on
its hosts via `simpler_objects_common`); any group that's empty or absent makes
its play a no-op:

| Play | Targets group    | What it does                                   |
|------|------------------|------------------------------------------------|
| #1   | `object_servers` | venv + per-disk object-server instance         |
| #2   | `locators`       | venv + the locator unit                        |
| #3   | `replicators`    | venv + the async-replicate timer               |

So if you want Ansible to manage only the object servers — and run the locator
and/or replication separately (by hand following
[`../systemd/README.md`](../systemd/README.md), with cron, on a different
machine, or not at all) — trim your inventory to a single group:

```yaml
# inventory/hosts.yml — object-server-only
all:
  vars:
    ansible_user: simpler-objects
    simpler_objects_version: v0.4.4
  children:
    object_servers:
      hosts:
        pi-storage-1.lan:
          object_directory: /mnt/extusb-a/simpler-objects/data
    locators:
      hosts: {}
    replicators:
      hosts: {}
```

And run the same playbook:

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

The locator and replicator plays will report `(0 hosts)` and move on.

## Upgrading

Bump `simpler_objects_version` in `inventory/hosts.yml` and re-run the whole
playbook:

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

pip detects the new tag, reinstalls into the venv, and notifies the restart
handler so the service comes up on the new code. Every other task is a no-op
when nothing changed, so the run is safe to repeat. (The async-replicate timer
isn't restarted — its next firing uses the new venv automatically, since the
service is `Type=oneshot`.)

A corrupted venv self-heals: the playbook probes `~/venv` for a loadable `pip`
and, if a system Python upgrade has broken it, removes and rebuilds it before
the pip step (issue #73). No manual `rm -rf ~/venv` needed.

## Privilege model

The playbook connects as the service user and runs entirely without root or
sudo. All files land under that user's home directory; systemd units are
managed via `systemctl --user`. The only root involvement is the one-time
prerequisite steps (packages, user creation, disk chown, linger) documented
above.

The `simpler_objects_common` role asserts that the prerequisites are in place
(python3, python3-venv, linger enabled for the connecting user) and fails with
a clear message if they are not, rather than attempting to install them.

## Layout

```
deploy/ansible/
├── README.md                       (this file)
├── ansible.cfg
├── inventory/hosts.example.yml
├── site.yml                        — top-level play
└── roles/
    ├── simpler_objects_common/             — prereq checks, venv, pip install
    ├── simpler_objects_object_server/      — env file + drop-in per instance
    ├── simpler_objects_locator/            — env file + unit + enable
    └── simpler_objects_async_replicate/    — env file + unit + timer + enable
```

The roles `copy:` unit files verbatim from `../systemd/` — there is no
duplication of unit content into Ansible templates. Edits to a `.service` file
in `../systemd/` flow through on the next playbook run.

## What's not managed

- Disk partitioning, formatting, mounting (do this in `/etc/fstab` yourself)
- Bucket directory creation
- `chown` of `OBJECT_DIRECTORY` — buckets are presumed to already be owned
  correctly; the role verifies and fails loudly otherwise
- Firewalling, TLS, reverse proxy
- Multi-locator HA
- User creation, package installation, or linger setup (root prerequisites)
