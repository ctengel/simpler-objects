"""Simpler Objects Locator API"""

import asyncio
import os
import random
from contextlib import asynccontextmanager
from typing import Annotated
import httpx
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import RedirectResponse, Response
from simpler_objects import auth
from simpler_objects.common import check_content_type_extension, filter_write_candidates

OBJECT_SERVERS = os.environ.get('OBJECT_SERVERS', 'http://localhost:46579/')
# Shared HMAC secret for signing object-server URLs; unset = no signing.
CLUSTER_SECRET = os.environ.get('CLUSTER_SECRET', '')
SIGNED_URL_TTL = int(os.environ.get('SIGNED_URL_TTL', str(auth.DEFAULT_TTL)))
# Path to the client API-key/permission TOML; unset = no client auth.
AUTH_CONFIG = os.environ.get('AUTH_CONFIG')

# Fallback Retry-After on a busy 503; matches the object server's own constant.
RETRY_AFTER = "64"

# Basic is advertised so browsers prompt for client-name/API-key; Bearer with
# the bare key is accepted equally.
WWW_AUTHENTICATE = 'Basic realm="simpler-objects", charset="UTF-8"'


def signed_suffix(operation, bucket, key=""):
    """Return the '?exp=…&sig=…' suffix for an object-server URL, or ''.

    Used both for the locator's own probes and for the Location it hands the
    client — the signature covers the operation, so the probe (HEAD) and the
    client's redirected request (GET or HEAD, both 'read') share one suffix.
    """
    if not CLUSTER_SECRET:
        return ""
    return "?" + auth.signed_query(CLUSTER_SECRET, operation, bucket, key, SIGNED_URL_TTL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the shared object-server HTTP client and load the auth config."""
    if AUTH_CONFIG and not CLUSTER_SECRET:
        # Refuse the half-secure trap: clients would authenticate here only
        # to be redirected to unsigned URLs anyone can request directly.
        raise RuntimeError(
            "AUTH_CONFIG is set but CLUSTER_SECRET is not; "
            "set CLUSTER_SECRET on the locator and object servers first")
    app.state.auth = auth.AuthConfig.load(AUTH_CONFIG) if AUTH_CONFIG else None
    # One pooled client for the whole app, reused across all requests.
    app.state.client = httpx.AsyncClient()
    yield
    await app.state.client.aclose()

app = FastAPI(lifespan=lifespan)


def require_permission(operation):
    """Dependency factory: authenticate the API key, authorize the bucket op.

    No-op when AUTH_CONFIG is unset. Reads the bucket from the matched path
    params so the same dependency serves object and bucket-level routes.
    """
    def dependency(request: Request):
        config = app.state.auth
        if config is None:
            return
        client_name = config.authenticate(request.headers.get('authorization'))
        if client_name is None:
            raise HTTPException(status_code=401,
                                headers={"WWW-Authenticate": WWW_AUTHENTICATE})
        if not config.allowed(client_name, request.path_params['bucket'], operation):
            raise HTTPException(status_code=403)
    return dependency

def object_servers(randomized=False):
    """Return a randomized list of object server URLs"""
    servers = OBJECT_SERVERS.split(',')
    if randomized:
        random.shuffle(servers)
    return servers

async def get_object_server_health(url: str):
    """Get the health of an object server"""
    client = app.state.client
    try:
        result = await client.get(url + 'health', timeout=1)
        result.raise_for_status()
    except httpx.HTTPError:
        return {'write': False, 'read': False, 'quota-available-bytes': 0, 'quota-used-bytes': 0, 'percent': 0}
    return result.json()

@app.get('/health')
async def healthcheck():
    """Return basic info on cluster health"""
    servers = object_servers()
    healths = await asyncio.gather(*[get_object_server_health(s) for s in servers])
    return {'servers': dict(zip(servers, healths))}

@app.api_route("/{bucket}/{key}", methods=["GET", "HEAD"],
               dependencies=[Depends(require_permission(auth.OP_READ))])
async def find_object(bucket: str, key: str):
    """Return a redirect to an existing object"""
    object_path = f"{bucket}/{key}"
    # Only an explicit 503 from a reachable server is evidence the object
    # exists (a PUT is in progress); a timeout or transport error is the
    # absence of an answer, not proof, so it must not escalate to 503 — unlike
    # head_bucket, which answers a coarser question and may treat any error as
    # 503. Escalating every transport failure would mask genuine 404s as
    # "retry forever" whenever any node in the fleet is flaky.
    busy = False
    retry_after = None
    client = app.state.client
    # One signature serves every probe and the final Location.
    suffix = signed_suffix(auth.OP_READ, bucket, key)
    # Sequential by design: randomised order spreads load; first healthy server wins.
    for server in object_servers(randomized=True):
        try:
            result = await client.head(server + object_path + suffix, timeout=1)
        except httpx.HTTPError:
            continue
        if result.status_code == 200:
            return RedirectResponse(url=server + object_path + suffix)
        if result.status_code == 503:
            busy = True
            retry_after = result.headers.get("Retry-After", retry_after)
    if busy:
        raise HTTPException(status_code=503,
                            headers={"Retry-After": retry_after or RETRY_AFTER})
    raise HTTPException(status_code=404)

@app.put("/{bucket}/{key}", dependencies=[Depends(require_permission(auth.OP_WRITE))])
async def add_object(bucket: str, key: str,
                     content_length: Annotated[int | None, Header()] = None,
                     content_type: Annotated[str | None, Header()] = None):
    """Return a redirect to a server that can handle an object request"""
    if content_length is None:
        raise HTTPException(status_code=411)
    if not check_content_type_extension(key, content_type):
        raise HTTPException(status_code=415)
    object_path = f"{bucket}/{key}"
    # TODO use caches of objects and servers but then double check vs checking everybody
    all_obj_servers = object_servers()
    client = app.state.client
    probe_suffix = signed_suffix(auth.OP_READ, bucket, key)

    async def check_exists(server):
        try:
            result = await client.head(server + object_path + probe_suffix, timeout=1)
            return server, result.status_code
        except httpx.HTTPError:
            return server, None

    # Health and existence are independent fan-outs over every server, so they
    # run concurrently. Existence must cover all servers, not just writable
    # candidates: the object must not already exist anywhere in the cluster.
    health_fut = asyncio.gather(*[get_object_server_health(s) for s in all_obj_servers])
    exist_fut = asyncio.gather(*[check_exists(s) for s in all_obj_servers])
    healths = await health_fut
    exist_results = await exist_fut

    health = dict(zip(all_obj_servers, healths))
    candidates = filter_write_candidates(health, content_length)
    for server, status in exist_results:
        if status == 404:
            continue
        if status in (200, 503):
            raise HTTPException(status_code=409)
        # None or unexpected status (e.g. 500): server broken or unreachable
        candidates.pop(server, None)

    bucket_suffix = signed_suffix(auth.OP_LIST, bucket)

    async def check_bucket(server):
        try:
            result = await client.head(server + bucket + "/" + bucket_suffix, timeout=1)
            result.raise_for_status()
            return server, True
        except httpx.HTTPError:
            return server, False

    # Final stage: verify the bucket exists on each surviving candidate. It must
    # follow the stage above — it depends on the pruned candidate set.
    bucket_results = await asyncio.gather(*[check_bucket(s) for s in list(candidates.keys())])
    for server, ok in bucket_results:
        if not ok:
            candidates.pop(server, None)

    if not candidates:
        raise HTTPException(507)
    server_to_upload = random.choices(list(candidates.keys()), list(candidates.values()))[0]
    return RedirectResponse(url=server_to_upload + object_path
                            + signed_suffix(auth.OP_WRITE, bucket, key))

@app.get("/")
def list_buckets():
    """List buckets — not permitted"""
    raise HTTPException(status_code=403)

@app.head("/{bucket}/", dependencies=[Depends(require_permission(auth.OP_LIST))])
async def head_bucket(bucket: str):
    """Check if a bucket exists on any server"""
    client = app.state.client
    suffix = signed_suffix(auth.OP_LIST, bucket)

    async def check_server(server):
        try:
            result = await client.head(server + bucket + "/" + suffix, timeout=8)
            return result.status_code
        except httpx.HTTPError:
            return None

    # All servers queried in parallel; any 200 means the bucket exists.
    statuses = await asyncio.gather(*[check_server(s) for s in object_servers()])

    if 200 in statuses:
        return Response(status_code=200)
    if any(s != 404 for s in statuses):
        # 503, not 502/504: the locator coordinates object servers but is not
        # itself a gateway/proxy, so gateway-specific codes don't apply.
        raise HTTPException(status_code=503)
    raise HTTPException(status_code=404)

@app.get("/{bucket}/", dependencies=[Depends(require_permission(auth.OP_LIST))])
async def list_bucket(bucket: str):
    """List all items in a bucket"""
    client = app.state.client
    suffix = signed_suffix(auth.OP_LIST, bucket)

    async def fetch_server(server):
        try:
            result = await client.get(server + bucket + '/' + suffix, timeout=16)
        except httpx.HTTPError:
            return server, None
        return server, result

    # All servers queried in parallel; gather preserves order so the merge
    # loop below sees results in the same fixed sequence every time.
    results = await asyncio.gather(*[fetch_server(s) for s in object_servers()])

    items = {}
    for server, result in results:
        if result is None or result.status_code not in (200, 404):
            # 503, not 502/504: the locator coordinates object servers but is
            # not itself a gateway/proxy, so gateway-specific codes don't apply.
            raise HTTPException(503)
        if result.status_code == 404:
            continue
        for key, value in result.json()['objects'].items():
            if key not in items:
                items[key] = value
                items[key]['locations'] = []
                items[key]['error'] = False
            else:
                for subk in ['size', 'directory', 'checksum']:
                    if items[key][subk] != value[subk]:
                        items[key]['error'] = True
                        items[key][subk] = None
            items[key]['locations'].append(server)
    return {'bucket': bucket, 'objects': items}
