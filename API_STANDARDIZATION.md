# API Standardization Analysis: Current vs. Proposed

## Summary

This document compares the current `object_server.py` implementation against the OpenAPI spec defined in `openapi.yaml`. It highlights what's already working, what needs changes, and a phased migration plan.

---

## Endpoint Comparison

### 1. **GET /health** ✅ Partially Aligned

| Aspect | Current | Proposed | Status |
|--------|---------|----------|--------|
| **Response Format** | `{read, write, available, percent}` | `{status, quota-used-bytes, quota-available-bytes}` | Phase 3 (breaking) |
| **Status Representation** | Boolean flags | Enum: `readwrite`, `readonly`, `notready` | Needed for #18 (PUT locking) |
| **Field Names** | Custom | WebDAV-compatible (RFC 4331) | Improves standardization |
| **Available Space** | Both `available` (bytes) and `percent` | `quota-available-bytes` only | Cleaner |

**Current behavior:**
```python
# Line 58-66 in object_server.py
@app.get('/health')
def healthcheck():
    disk_stats = shutil.disk_usage(pathlib.Path(OBJECT_DIRECTORY))
    r = {'read': True,         # ← Always true in current impl
         'write': True,        # ← Always true in current impl
         'available': disk_stats.free,
         'percent': int(float(disk_stats.free)/float(disk_stats.total)*100.0)}
    return r
```

**Action Items:**
- **Phase 3**: Add `status` field to response (non-breaking: keep old fields)
- Set `status: notready` when PUT in progress (fixes #18)
- Eventually deprecate boolean flags

---

### 2. **GET /{bucket}/{key}** ✅ Well Aligned

| Aspect | Current | Proposed | Status |
|--------|---------|----------|--------|
| **Headers** | `Repr-Digest` | `Content-Digest` (RFC 9162) | Phase 1 (add alongside) |
| **Range Requests** | ❌ Not supported | ✅ RFC 7233 | Phase 2 (new) |
| **Content-Type** | ❌ Not returned | ✅ Returned | Phase 1 (new) |
| **Response Format** | FileResponse (binary) | Same | ✅ No change |
| **Status Codes** | 200, 404 | 200, 206 (ranges), 416, 404 | Phase 2 (adds 206, 416) |

**Current behavior:**
```python
# Line 68-87 in object_server.py
@app.api_route("/{bucket}/{key}", methods=['GET', 'HEAD'])
async def get_object(bucket: str, key: str):
    path = object_filename(bucket, key)
    if not path.is_file():
        raise HTTPException(status_code=404)
    
    # Load checksum from disk
    my_cksum = None
    try:
        with open(checksum_filename(path.parent), encoding='utf-8') as fp:
            for line in fp:
                checksum, file_name = line.strip().split()
                if file_name == key:
                    my_cksum = bytes.fromhex(checksum)
                    break
    except FileNotFoundError:
        pass
    
    headers = None
    if my_cksum:
        headers = {"Repr-Digest": http_digest_head(my_cksum)}  # ← RFC 8949 format
    return FileResponse(path, headers=headers)
```

**Action Items:**
- **Phase 1**: Add `Content-Digest` header (duplicate of `Repr-Digest` initially)
- **Phase 1**: Add `Content-Type` header (need MIME type detection)
- **Phase 2**: Implement RFC 7233 Range request support
- No breaking changes needed

---

### 3. **PUT /{bucket}/{key}** ✅ Core Logic Solid, Needs Headers

| Aspect | Current | Proposed | Status |
|--------|---------|----------|--------|
| **Digest Verification** | ✅ Uses `Content-Digest` and `Repr-Digest` headers | ✅ RFC 9162 standard | Already aligned |
| **Content-Length** | ✅ Required, validated | ✅ Required | ✅ No change |
| **Conflict Handling** | 409 if exists | 409 if exists | ✅ No change |
| **Checksum Return** | `Repr-Digest` | Also add `Content-Digest` | Phase 1 |
| **Content-Type** | ❌ Not stored | ✅ Accept header, store metadata | Phase 1 (enhancement) |
| **Locking** | ❌ No locking | ✅ Set `status: notready` during upload | Phase 3 (#18) |

**Current behavior:**
```python
# Line 89-129 in object_server.py
@app.put("/{bucket}/{key}")
async def put_object(bucket: str, key: str, request: Request):
    # Parse Content-Length
    length = int(request.headers["Content-Length"])
    
    # Prevent overwrites
    path = object_filename(bucket, key)
    if path.exists():
        raise HTTPException(status_code=409)  # ← Correct
    
    # Parse digest from headers
    request_digest = parse_digest_headers(request.headers)  # ← Handles both Repr-Digest and Content-Digest
    
    # Receive file
    with open(path, "wb") as dst:
        async for chunk in request.stream():
            dst.write(chunk)
    
    # Hash and verify
    file_digest = file_checksum(path)
    if request_digest and file_digest != request_digest:
        path.unlink()
        raise HTTPException(status_code=400)  # ← Correct
    
    # Write checksum to disk
    hash_file = checksum_filename(path.parent)
    cksum_line = f"{file_digest.hex()}  {path.name}\n"
    with open(hash_file, 'a', encoding='utf-8') as hf:
        hf.write(cksum_line)
    
    return Response(status_code=201, content=None,
                    headers={"Repr-Digest": http_digest_head(file_digest)})
```

**Action Items:**
- **Phase 1**: Add `Content-Digest` to response headers
- **Phase 1**: Accept and validate `Content-Type` header
- **Phase 3**: Implement locking (set health status to `notready` during upload)
- Core logic already solid — no breaking changes required

---

### 4. **GET /{bucket}/** ✅ Well Aligned with Minor Improvements

| Aspect | Current | Proposed | Status |
|--------|---------|----------|--------|
| **Response Format** | `{bucket, objects: {...}}` | Same | ✅ No change |
| **Object Fields** | `directory`, `size`, `checksum` | Same + future `last-modified` | ✅ Backward compatible |
| **Pagination** | ❌ None (lists all) | ✅ Proposed `marker`, `prefix` | Phase 4 (optional) |
| **MIME Type** | ❌ Not returned | ✅ Proposed | Phase 4 (optional) |
| **Replica Info** | N/A (locator API feature) | `locations`, `error` | Locator API only |

**Current behavior:**
```python
# Line 133-158 in object_server.py
@app.api_route("/{bucket}/", methods=['GET', 'HEAD'])
def list_directory(bucket: str):
    dir_path = pathlib.Path(OBJECT_DIRECTORY).joinpath(bucket)
    if not dir_path.is_dir():
        raise HTTPException(status_code=404)
    
    r = {"bucket": bucket, "objects": {}}
    
    # Load all checksums
    hashes = {}
    try:
        with open(checksum_filename(dir_path), encoding='utf-8') as fp:
            for line in fp:
                checksum, file_name = line.strip().split()
                hashes[file_name] = checksum
    except FileNotFoundError:
        pass
    
    # Build object list
    for name in dir_path.iterdir():
        if name.is_dir():
            r['objects'][name.name] = {'directory': True, 'size': 0, 'checksum': None}
        else:
            r['objects'][name.name] = {
                'directory': False,
                'size': name.stat().st_size,
                'checksum': hashes.get(name.name)
            }
    return r
```

**Action Items:**
- ✅ No immediate changes needed (already aligned)
- **Phase 4**: Add `marker` and `prefix` parameters for pagination
- **Phase 4**: Return MIME type per object
- **Phase 4**: Add `last-modified` timestamp

---

## Header Standardization Details

### Digest Headers (Phase 1 - Non-Breaking)

**Current:**
- Uses `Repr-Digest` header in RFC 8949 format: `sha-256=:<base64>:`

**Proposed:**
- Support both `Repr-Digest` (old) and `Content-Digest` (RFC 9162, new)
- Server should accept both on PUT requests
- Server should return both on GET/PUT responses (for compatibility)

**Migration:**
```python
# Current code already handles both! (Line 40-48)
def parse_digest_headers(headers: dict):
    """Get one SHA-256 from multiple headers"""
    options = set(parse_digest_header(headers.get(x)) for x in ['Repr-Digest', 'Content-Digest'])
    options.discard(None)
    if len(options) > 1:
        raise HTTPException(status_code=400)
    if len(options) == 0:
        return None
    return options.pop()
```

✅ **Good news**: Code already validates both headers. Just need to:
1. Return both headers in responses
2. Document preference for `Content-Digest` in new code

---

## Locator API Comparison

### Current Behavior (locator_api.py)

```python
@app.get('/health')
def healthcheck():
    return {'servers': {x: get_object_server_health(x) for x in object_servers()}}
    # Returns: {servers: {url: {read, write, available, percent}, ...}}

@app.get("/{bucket}/")
def list_bucket(bucket: str):
    # Aggregates listings from all servers
    # Adds: locations (which servers have it), error (mismatch flag)
    return {'bucket': bucket, 'objects': items}
```

### Changes Needed

| Feature | Current | Proposed | Phase |
|---------|---------|----------|-------|
| **Server Health Response** | Mirrors object server format | Will change in Phase 3 | Phase 3 |
| **Replica Tracking** | Aggregates `locations` | Same | ✅ Compatible |
| **Mismatch Detection** | Flags with `error` | Same | ✅ Compatible |
| **New Status Enum** | Will see boolean flags | Will see `status` enum | Phase 3 |

**Phase 3 change:**
```python
# Current
'servers': {'http://localhost:29171/': {'read': True, 'write': True, 'available': ..., 'percent': ...}}

# Proposed (Phase 3)
'servers': {'http://localhost:29171/': {'status': 'readwrite', 'quota-available-bytes': ..., ...}}
```

---

## Migration Phases

### Phase 1: Add Standard Headers (Non-Breaking) 🟢
- **Files**: `simpler_objects/object_server.py`
- **Changes**:
  - Return both `Repr-Digest` and `Content-Digest` headers on GET/PUT
  - Add `Content-Type` header on GET (detect MIME type from key extension)
  - Document new headers in responses
- **Backward Compatibility**: ✅ Fully backward compatible (adding headers, not changing)
- **Timeline**: Can do immediately

### Phase 2: Implement Range Requests (New Capability) 🟡
- **Files**: `simpler_objects/object_server.py`
- **Changes**:
  - Parse `Range` header on GET requests
  - Return 206 Partial Content with `Content-Range` header
  - Return 416 Range Not Satisfiable for invalid ranges
- **Backward Compatibility**: ✅ Fully backward compatible (new feature)
- **Timeline**: Needed for #26 (large file upload optimization)
- **Complexity**: Medium (RFC 7233 implementation)

### Phase 3: Standardize Health Response (Breaking) 🔴
- **Files**: `simpler_objects/object_server.py`, `simpler_objects/locator_api.py`, clients
- **Changes**:
  - Add `status` field to health response
  - Eventually deprecate `read`/`write` boolean fields
  - Update locator API to handle new enum
  - Use `status: notready` during PUT operations (fixes #18)
- **Backward Compatibility**: ⚠️ Breaking change (clients need update)
- **Timeline**: After Phase 1 & 2 are stable
- **Dependencies**: Fixes #18 (lock if PUT in progress)

### Phase 4: Enhanced Bucket Listing (New Capability) 🟡
- **Files**: `simpler_objects/object_server.py`
- **Changes**:
  - Add `prefix` and `marker` parameters for pagination
  - Add `last-modified` timestamp per object
  - Optionally return MIME type per object
- **Backward Compatibility**: ✅ Fully backward compatible (optional parameters)
- **Timeline**: Future optimization

---

## Issues Resolved by This Work

| Issue | Phase | How |
|-------|-------|-----|
| #14 (Make API simple/standard) | 1-4 | OpenAPI spec + standardized headers + enum |
| #18 (lock if PUT in progress) | 3 | `status: notready` during upload |
| #25 (atomic writes) | TBD | Related to locking, may need transactions |
| #26 (Is put done twice?) | 2 | Range request support prevents unnecessary re-uploads |

---

## Testing Strategy

### Phase 1 Tests
- [ ] GET request returns both `Repr-Digest` and `Content-Digest`
- [ ] PUT request accepts both digest headers
- [ ] Content-Type is correctly detected and returned
- [ ] Clients can consume new headers without breaking

### Phase 2 Tests
- [ ] Range requests return 206 with `Content-Range`
- [ ] Invalid ranges return 416
- [ ] Partial content matches expected bytes
- [ ] `Repr-Digest` covers full file, not just range

### Phase 3 Tests
- [ ] Health response includes `status` field
- [ ] `status: notready` is set during PUT
- [ ] Locator API gracefully handles new enum
- [ ] Clients can update to use enum

### Phase 4 Tests
- [ ] Pagination parameters work correctly
- [ ] `last-modified` is accurate
- [ ] MIME types are correctly detected

---

## Files to Modify

```
simpler_objects/
├── object_server.py          ← Main changes (Phases 1-3)
├── locator_api.py            ← Phase 3 changes (handle new health format)
├── tests/
│   ├── test_object_server.py ← Add tests for all phases
│   └── test_locator_api.py   ← Add tests for Phase 3
└── (new) openapi.yaml        ← ✅ Created (spec document)
```

---

## Quick Reference: Summary Table

| Endpoint | Method | Current Status | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|----------|--------|----------------|---------|---------|---------|---------|
| `/health` | GET | ✅ Works | 📝 Add status field | — | 🔄 Breaking update | — |
| `/{bucket}/{key}` | GET | ✅ Works | 📝 Add headers | 🆕 Range support | — | — |
| `/{bucket}/{key}` | HEAD | ✅ Works | 📝 Add headers | 🆕 Range support | — | — |
| `/{bucket}/{key}` | PUT | ✅ Works | 📝 Add headers | — | 📝 Add locking | — |
| `/{bucket}/` | GET | ✅ Works | — | — | — | 🆕 Pagination |
| `/{bucket}/` | HEAD | ✅ Works | — | — | — | — |

Legend: ✅ = Implemented, 📝 = Enhance, 🆕 = New, 🔄 = Breaking change, — = No change

