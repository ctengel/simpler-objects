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

## ObjectIndex

Optionally on ports 46569 (API) and 46567 (GUI).  Will be lowered/changed in future.
