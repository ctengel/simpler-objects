# simpler-objects
A simpler object storage service

## Start storage servers

For each filesystem/drive

```
python -m http.server -d /path/to/objects 46579
```

## Start object locator

```
OBJECT_SERVERS="http://localhost:46579/" fastapi dev --port 46752 simpler_objects/locator_api.py
```

## ObjectIndex

Optionally on ports 46569 (API) and 46567 (GUI)
