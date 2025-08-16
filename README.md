# simpler-objects
A simpler object storage service

## Start storage servers

For each filesystem/drive

```
python -m simpler_objects.object_server -d /path/to/objects 46579
```

## Start object locator

Do this once

```
OBJECT_SERVERS="http://localhost:46579/" fastapi dev --port 46752 simpler_objects/locator_api.py
```

## Use

PUT an object

```
curl -L -T /path/to/file http://localhost:46572/object_key
```

GET an object:
```
curl -L http://localhost:46572/object_key
```

## ObjectIndex

Optionally on ports 46569 (API) and 46567 (GUI)
