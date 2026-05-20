# simpler-objects
A simpler object storage service

## Start storage servers

For each filesystem/drive

```
OBJECT_DIRECTORY=/path/to/objects fastapi dev --port 29171 simpler_objects/object_server.py
```

Alternatively `uvicorn simpler_objects.object_server:app`.

## Start object locator

Do this once

```
OBJECT_SERVERS="http://localhost:29171/" fastapi dev --port 29164 simpler_objects/locator_api.py
```

## Use

PUT an object

```
curl -L -T /path/to/file http://localhost:29164/object_key
```

GET an object:
```
curl -L http://localhost:29164/object_key
```

## Replication

Start up another object server:

```
OBJECT_DIRECTORY=/path/to/more-objects fastapi dev --port 29172 simpler_objects/object_server.py
```

Restart locator with second object server included:

```
OBJECT_SERVERS="http://localhost:29171/,http://localhost:29172/" fastapi dev --port 29164 simpler_objects/locator_api.py
```

Run an asynchronous replication periodically:
```
python -m simpler_objects.async_replicate http://localhost:29164/ bucket 2
```

## Validation

```
cd /path/to/bucket
sha256sum -c ../bucket.sha256
```

Note that ObjectIndex has a legacy way to manage this. Simply move your old files into above space and it should "just work."

## Performance test

```
./perf_test.sh http://localhost:29171/ big_file small_file
```

## Setting up storage servers

- Ensure your disks are aligned on 4k (or 1MB?) boundaries etc
- create a seperate user `useradd -m`
- create buckets at top level with date `mkdir /path/to/bucket-20000101`
- run in screen
- change `fastapi dev` to `fastapi run` for prod

## Client guidance

### Redirects (307)

The locator returns `307 Temporary Redirect` for `GET`, `HEAD`, and `PUT` on `/{bucket}/{key}`. Clients must follow redirects and must preserve the original HTTP method — particularly for PUT. Use `curl -L`; verify your HTTP library honours 307 method preservation for non-GET requests.

### `Content-Length` on PUT

`Content-Length` is optional but highly recommended and may become mandatory again in a future version. Sending it allows the locator to select a server with sufficient free space (if omitted, 1 GiB is assumed) and allows the object server to verify the upload was received intact.

### Digest headers on PUT

Clients may send `Content-Digest` or `Repr-Digest` with a SHA-256 value (`sha-256=:base64:` format) for integrity verification. The object server returns `400` on mismatch. The `Repr-Digest` header on GET/HEAD responses is present only when a checksum record exists for the object — do not assume it is always included.

### Changes in spec v0.2.0

- **`HEAD /{bucket}/` on the locator** is now supported (previously returned `405`). Returns `200` if the bucket exists on any server, `404` if none have it, `503` for server errors.
- **Directory entries in `GET /{bucket}/`** now return `"size": null` instead of `"size": 0`. Use the `"directory": true` field to identify directories rather than testing `size == 0`.

## ObjectIndex

Optionally on ports 46569 (API) and 46567 (GUI).  Will be lowered/changed in future.
