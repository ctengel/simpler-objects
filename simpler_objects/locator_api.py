"""Simpler Objects Locator API"""

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import Annotated
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import RedirectResponse, Response
from simpler_objects.common import filter_write_candidates
from simpler_objects.logging_config import (
    configure,
    install_request_id_middleware,
    request_id_var,
)

configure()
logger = logging.getLogger(__name__)

OBJECT_SERVERS = os.environ.get('OBJECT_SERVERS', 'http://localhost:46579/')

# Fallback Retry-After on a busy 503; matches the object server's own constant.
RETRY_AFTER = "64"


def _outbound_headers() -> dict:
    """Headers to forward on outbound httpx calls; propagates the request ID."""
    rid = request_id_var.get()
    return {"X-Request-Id": rid} if rid else {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the lifecycle of the shared object-server HTTP client."""
    # One pooled client for the whole app, reused across all requests.
    app.state.client = httpx.AsyncClient()
    yield
    await app.state.client.aclose()

app = FastAPI(lifespan=lifespan)
install_request_id_middleware(app)

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
        result = await client.get(url + 'health', timeout=1, headers=_outbound_headers())
        result.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("locator.health.unreachable",
                     extra={"server": url, "error": str(exc)})
        return {'write': False, 'read': False, 'quota-available-bytes': 0, 'quota-used-bytes': 0, 'percent': 0}
    return result.json()

@app.get('/health')
async def healthcheck():
    """Return basic info on cluster health"""
    servers = object_servers()
    healths = await asyncio.gather(*[get_object_server_health(s) for s in servers])
    return {'servers': dict(zip(servers, healths))}

@app.api_route("/{bucket}/{key}", methods=["GET", "HEAD"])
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
    busy_servers: list[str] = []
    probed: dict[str, int | None] = {}
    client = app.state.client
    # Sequential by design: randomised order spreads load; first healthy server wins.
    for server in object_servers(randomized=True):
        try:
            result = await client.head(server + object_path, timeout=1,
                                       headers=_outbound_headers())
        except httpx.HTTPError as exc:
            probed[server] = None
            logger.debug("locator.find.probe.error",
                         extra={"server": server, "key": object_path,
                                "error": str(exc)})
            continue
        probed[server] = result.status_code
        logger.debug("locator.find.probe",
                     extra={"server": server, "key": object_path,
                            "status": result.status_code})
        if result.status_code == 200:
            logger.info("locator.find.redirect",
                        extra={"bucket": bucket, "key": key, "server": server})
            return RedirectResponse(url=server + object_path)
        if result.status_code == 503:
            busy = True
            busy_servers.append(server)
            retry_after = result.headers.get("Retry-After", retry_after)
    if busy:
        logger.warning("locator.find.busy",
                       extra={"bucket": bucket, "key": key,
                              "busy_servers": busy_servers, "probed": probed})
        raise HTTPException(status_code=503,
                            headers={"Retry-After": retry_after or RETRY_AFTER})
    logger.info("locator.find.missing",
                extra={"bucket": bucket, "key": key, "probed": probed})
    raise HTTPException(status_code=404)

@app.put("/{bucket}/{key}")
async def add_object(bucket: str, key: str, content_length: Annotated[int | None, Header()] = None):
    """Return a redirect to a server that can handle an object request"""
    if content_length is None:
        logger.warning("locator.put.no_content_length",
                       extra={"bucket": bucket, "key": key})
        raise HTTPException(status_code=411)
    object_path = f"{bucket}/{key}"
    # TODO use caches of objects and servers but then double check vs checking everybody
    all_obj_servers = object_servers()
    client = app.state.client

    async def check_exists(server):
        try:
            result = await client.head(server + object_path, timeout=1,
                                       headers=_outbound_headers())
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
    logger.debug("locator.put.candidates",
                 extra={"bucket": bucket, "key": key,
                        "content_length": content_length,
                        "writable": list(candidates.keys()),
                        "exist_results": dict(exist_results)})
    for server, status in exist_results:
        if status is None:
            candidates.pop(server, None)
        elif status != 404:
            logger.warning("locator.put.conflict",
                           extra={"bucket": bucket, "key": key,
                                  "existing_server": server, "status": status})
            raise HTTPException(status_code=409)

    async def check_bucket(server):
        try:
            result = await client.head(server + bucket + "/", timeout=1,
                                       headers=_outbound_headers())
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
        logger.error("locator.put.no_space",
                     extra={"bucket": bucket, "key": key,
                            "content_length": content_length,
                            "bucket_results": dict(bucket_results)})
        raise HTTPException(507)
    chosen = list(candidates.keys())
    weights = list(candidates.values())
    server_to_upload = random.choices(chosen, weights)[0]
    logger.info("locator.put.select",
                extra={"bucket": bucket, "key": key,
                       "server": server_to_upload,
                       "content_length": content_length,
                       "candidates": chosen,
                       "weight": candidates[server_to_upload]})
    return RedirectResponse(url=server_to_upload+object_path)

@app.get("/")
def list_buckets():
    """List buckets — not permitted"""
    raise HTTPException(status_code=403)

@app.head("/{bucket}/")
async def head_bucket(bucket: str):
    """Check if a bucket exists on any server"""
    client = app.state.client

    async def check_server(server):
        try:
            result = await client.head(server + bucket + "/", timeout=2,
                                       headers=_outbound_headers())
            return result.status_code
        except httpx.HTTPError:
            return None

    # All servers queried in parallel; any 200 means the bucket exists.
    servers = object_servers()
    statuses = await asyncio.gather(*[check_server(s) for s in servers])
    by_server = dict(zip(servers, statuses))

    if 200 in statuses:
        return Response(status_code=200)
    if any(s != 404 for s in statuses):
        # 503, not 502/504: the locator coordinates object servers but is not
        # itself a gateway/proxy, so gateway-specific codes don't apply.
        logger.warning("locator.bucket.upstream_error",
                       extra={"bucket": bucket, "op": "head",
                              "statuses": by_server})
        raise HTTPException(status_code=503)
    raise HTTPException(status_code=404)

@app.get("/{bucket}/")
async def list_bucket(bucket: str):
    """List all items in a bucket"""
    client = app.state.client

    async def fetch_server(server):
        try:
            result = await client.get(server + bucket + '/', timeout=2,
                                      headers=_outbound_headers())
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
            logger.warning("locator.bucket.upstream_error",
                           extra={"bucket": bucket, "op": "list",
                                  "server": server,
                                  "status": result.status_code if result else None})
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
