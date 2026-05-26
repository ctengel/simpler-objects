# Ansible deployment

Ansible wrapper around the units in [`../systemd/`](../systemd/). Drives the
full simpler-objects stack — object servers, locator, and the async-replicate
timer — onto a fleet of Raspberry Pis (or any Debian-family host), with version
upgrades reduced to a one-line YAML edit.

## What this assumes about the target hosts

- Raspberry Pi OS trixie (or bookworm with Python 3.12 manually installed), or
  any other Debian-family OS with `apt` and Python 3.12 available
- Networking is up; you can SSH in as a user with passwordless `sudo`
- Storage disks are already partitioned, formatted, mounted (via `/etc/fstab`),
  and contain the bucket directories you want to serve. The Ansible role does
  **not** manage mounts or buckets — that's `/etc/fstab` territory
- The mount path can be anywhere (e.g. `/mnt/<arbitrary-name>/simpler-objects/data`);
  the role generates a per-instance systemd drop-in that points
  `RequiresMountsFor=` and `ReadWritePaths=` at whatever path you put in
  `object_directory`

## What this manages on the target hosts

- The `simpler-objects` system user
- `/opt/simpler-objects/venv` and the pip-installed package at a pinned git tag
- `/etc/simpler-objects/*.env` per-service config files (mode 0640, owned
  `root:simpler-objects`)
- The systemd units (copied verbatim from `../systemd/`) under
  `/etc/systemd/system/`
- Per-object-server-instance drop-ins under
  `/etc/systemd/system/simpler-objects-object-server@<instance>.service.d/`
- Enabling and starting the units

## Requirements (on the control node — your laptop)

- `ansible-core` ≥ 2.15
- SSH access to the Pis as a user with passwordless sudo

## Quickstart

```bash
cd deploy/ansible
cp inventory/hosts.example.yml inventory/hosts.yml
$EDITOR inventory/hosts.yml                              # set hosts, disks, ports

ansible-playbook --syntax-check site.yml                 # sanity check
ansible-playbook -i inventory/hosts.yml site.yml --check --diff   # dry run
ansible-playbook -i inventory/hosts.yml site.yml         # do it
```

After a real run:

```bash
ssh pi-storage-1.lan systemctl is-active simpler-objects-object-server@disk1
curl http://pi-storage-1.lan:29171/health
curl http://pi-coord.lan:29164/health
ssh pi-coord.lan systemctl list-timers simpler-objects-async-replicate.timer
```

## Inventory shape

See [`inventory/hosts.example.yml`](inventory/hosts.example.yml) for an
annotated example with two storage Pis and a coordinator Pi. The four knobs
the playbook exposes:

| Variable                            | Where        | Effect                                   |
|-------------------------------------|--------------|------------------------------------------|
| `simpler_objects_version`           | `all.vars`   | Git tag to pip-install. Bump to upgrade. |
| `object_server_instances[].read_only` | per host   | `true` → RO mirror; default `false`      |
| `object_server_instances[].port`    | per instance | uvicorn listen port; default `29171`     |
| `object_server_instances[].workers` | per instance | uvicorn worker count; default `1`        |

## Partial deployment — managing one service only

You don't have to use Ansible for all three services. The four plays in
`site.yml` each target a single inventory group; any group that's empty or
absent makes its play a no-op:

| Play | Targets group  | What it does                                   |
|------|----------------|------------------------------------------------|
| #1   | union of all 3 | Install venv on every managed host             |
| #2   | `object_servers` | Configure per-disk instances                 |
| #3   | `locators`     | Configure the locator unit                     |
| #4   | `replicators`  | Configure the async-replicate timer            |

So if you want Ansible to manage only the object servers — and run the locator
and/or replication separately (by hand following
[`../systemd/README.md`](../systemd/README.md), with cron, on a different
machine, or not at all) — trim your inventory to a single group:

```yaml
# inventory/hosts.yml — object-server-only
all:
  vars:
    simpler_objects_version: v0.4.0
  children:
    object_servers:
      hosts:
        pi-storage-1.lan:
          object_server_instances:
            - name: disk1
              object_directory: /mnt/extusb-a/simpler-objects/data
    # Declared but empty — keeps the host pattern in play #1 quiet. You can
    # also omit these two groups entirely; you'll just get a one-line warning
    # per missing group ("Could not match supplied host pattern").
    locators:
      hosts: {}
    replicators:
      hosts: {}
```

And run the same playbook:

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

Plays #3 and #4 will report `(0 hosts)` and move on. `--tags update` works the
same way against a trimmed inventory.

If your inventory does contain all three groups but you want a single run to
touch only one service, use `--limit`:

```bash
ansible-playbook -i inventory/hosts.yml site.yml --limit object_servers
ansible-playbook -i inventory/hosts.yml site.yml --limit locators --tags update
```

The same applies in reverse: a locator-only or replicate-only inventory works
identically — fill in the group you want and leave the others empty.

## Upgrading

Bump `simpler_objects_version` in `inventory/hosts.yml`, then:

```bash
ansible-playbook -i inventory/hosts.yml site.yml --tags update
```

`--tags update` runs only the pip-install task and the restart handlers. The
async-replicate timer doesn't need a restart — its next firing uses the new
venv automatically (`Type=oneshot`).

To force a reinstall at the same tag (e.g. you suspect a corrupted venv):

```bash
ansible-playbook -i inventory/hosts.yml site.yml --tags update \
    -e simpler_objects_force_reinstall=true
```

## Privilege model

The playbook uses `become: true` only where root is genuinely required: apt,
useradd, writes under `/opt` and `/etc`, daemon-reload, `systemctl enable`.
The pip install task runs as `become_user: simpler-objects` so the venv is
owned by the service account, not by root. The SSH user you log in as does
not need to be root; standard passwordless sudo is enough.

## Layout

```
deploy/ansible/
├── README.md                       (this file)
├── ansible.cfg
├── inventory/hosts.example.yml
├── site.yml                        — top-level play
└── roles/
    ├── simpler_objects_common/             — apt deps, user, venv, pip install
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
