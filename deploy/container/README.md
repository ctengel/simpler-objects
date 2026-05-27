# Container (locator)

OCI-compliant image for the **locator only**. Build runs against any Docker- or Podman-compatible builder; the produced image is `~165 MB` (debian-slim base + Python runtime + fastapi/uvicorn/httpx).

## Why no object-server image

The object server's storage is a persistent disk mounted at a host path (the Pi+USB case from issue #17). Putting that behind a container adds bind-mount and uid-mapping complexity for zero operational benefit over the systemd template in `../systemd/`. The locator is stateless and a natural fit for containerisation; the object server is not.

## Build

```
podman build -t simpler-objects-locator -f deploy/container/Containerfile .
# or
docker build -t simpler-objects-locator -f deploy/container/Containerfile .
```

Multi-stage build: a `python:3.12-slim` build stage runs `pip install --prefix=/install .`, then the runtime stage copies that prefix into a fresh `python:3.12-slim` and drops to an unprivileged user. `pycurl` is in the `[client]` extra (not installed in this image) so the runtime needs no `libcurl` system package.

## Run

```
podman run --rm -d --name locator \
    -p 29164:29164 \
    -e OBJECT_SERVERS=http://pi1.lan:29171/,http://pi2.lan:29171/ \
    simpler-objects-locator
```

### Configuration

All knobs are env vars (matching the systemd unit's conventions):

| Variable | Default | Purpose |
|---|---|---|
| `OBJECT_SERVERS` | — (no default; required) | Comma-separated locator backends; trailing slash required |
| `HOST` | `0.0.0.0` | Bind address inside the container |
| `PORT` | `29164` | Bind port inside the container |
| `WORKERS` | `1` | uvicorn worker count |

`OBJECT_SERVERS` has no default because there is no sensible one — the container needs to know how to reach the object servers on your network.

### Healthcheck

```
curl http://<host>:29164/health
```

The shipped image does not declare a `HEALTHCHECK` instruction: the locator's `/health` makes outbound calls to every configured object server and can be slow under partial cluster failure; whether that should mark the container "unhealthy" is an orchestrator-level policy. Add `--health-cmd 'curl -fsS http://localhost:29164/health || exit 1'` at run time if your environment wants one.

## Signals and lifecycle

`CMD` is `sh -c 'exec uvicorn …'` — the `exec` makes uvicorn PID 1 inside the container, so `SIGTERM` from the container runtime reaches uvicorn directly and triggers a clean shutdown (no 10-second SIGKILL wait).

## Run alongside object servers on a Pi

The locator container can run on any host. If you co-locate it with an object server on the same Pi, expose the container to the host network or set `OBJECT_SERVERS=http://host.containers.internal:29171/` (podman) / `http://host.docker.internal:29171/` (docker) so the locator can reach the systemd-managed object server.
