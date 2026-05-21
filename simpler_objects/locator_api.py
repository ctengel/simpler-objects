"""Simpler Objects Locator API"""

import asyncio
import os
import random
from typing import Annotated
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import RedirectResponse, Response

app = FastAPI()

OBJECT_SERVERS = os.environ.get('OBJECT_SERVERS', 'http://localhost:46579/')

def object_servers(randomized=False):
    """Return a randomized list of object server URLs"""
    servers = OBJECT_SERVERS.split(',')
    if randomized:
        random.shuffle(servers)
    return servers

async def get_object_server_health(client: httpx.AsyncClient, url: str):
    """Get the health of an object server"""
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
    async with httpx.AsyncClient() as client:
        healths = await asyncio.gather(*[get_object_server_health(client, s) for s in servers])
    return {'servers': dict(zip(servers, healths))}

@app.api_route("/{bucket}/{key}", methods=["GET", "HEAD"])
async def find_object(bucket: str, key: str):
    """Return a redirect to an existing object"""
    object_path = f"{bucket}/{key}"
    # Sequential by design: randomised order spreads load; first healthy server wins.
    async with httpx.AsyncClient() as client:
        for server in object_servers(randomized=True):
            try:
                result = await client.head(server + object_path, timeout=1)
                result.raise_for_status()
            except httpx.HTTPError:
                continue
            return RedirectResponse(url=server+object_path)
    raise HTTPException(status_code=404)

@app.put("/{bucket}/{key}")
async def add_object(bucket: str, key: str, content_length: Annotated[int | None, Header()] = None):
    """Return a redirect to a server that can handle an object request"""
    if content_length is None:
        raise HTTPException(status_code=411)
    object_path = f"{bucket}/{key}"
    # TODO use caches of objects and servers but then double check vs checking everybody
    all_obj_servers = object_servers()
    # Three sequential phases, each parallelised across servers.
    # Each phase feeds the next, so they cannot be collapsed into one gather.
    async with httpx.AsyncClient() as client:
        # Phase 1: health — builds the initial candidates set.
        healths = await asyncio.gather(*[get_object_server_health(client, s) for s in all_obj_servers])
        health = dict(zip(all_obj_servers, healths))
        candidates = {server: stats['quota-available-bytes'] * stats['percent']
                      for server, stats in health.items()
                      if stats['write'] and stats['percent'] > 1
                      and stats['quota-available-bytes'] > content_length + 1024*1024}

        async def check_exists(server):
            try:
                result = await client.head(server + object_path, timeout=1)
                return server, result.status_code
            except httpx.HTTPError:
                return server, None

        # Phase 2: existence — checks all servers, not just candidates, because
        # the object must not exist anywhere in the cluster regardless of whether
        # a server is currently writable. Unreachable servers are dropped from candidates.
        exist_results = await asyncio.gather(*[check_exists(s) for s in all_obj_servers])
        for server, status in exist_results:
            if status is None:
                candidates.pop(server, None)
            elif status != 404:
                raise HTTPException(status_code=409)

        async def check_bucket(server):
            try:
                result = await client.head(server + bucket + "/", timeout=1)
                result.raise_for_status()
                return server, True
            except httpx.HTTPError:
                return server, False

        # Phase 3: bucket — verifies the target bucket exists on each remaining candidate.
        bucket_results = await asyncio.gather(*[check_bucket(s) for s in list(candidates.keys())])
        for server, ok in bucket_results:
            if not ok:
                candidates.pop(server, None)

    if not candidates:
        raise HTTPException(507)
    server_to_upload = random.choices(list(candidates.keys()), list(candidates.values()))[0]
    return RedirectResponse(url=server_to_upload+object_path)

@app.get("/")
def list_buckets():
    """List buckets — not permitted"""
    raise HTTPException(status_code=403)

@app.head("/{bucket}/")
async def head_bucket(bucket: str):
    """Check if a bucket exists on any server"""
    async with httpx.AsyncClient() as client:
        async def check_server(server):
            try:
                result = await client.head(server + bucket + "/", timeout=2)
                return result.status_code
            except httpx.HTTPError:
                return None

        # All tasks start in parallel. asyncio.wait(FIRST_COMPLETED) lets us return
        # as soon as any server confirms 200, cancelling the rest rather than waiting
        # for the slowest one. asyncio.gather would always wait for all of them.
        tasks = {asyncio.create_task(check_server(s)) for s in object_servers()}
        error = False
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.result() == 200:
                    for pending in tasks:
                        pending.cancel()
                    return Response(status_code=200)
                if t.result() is None or t.result() != 404:
                    error = True

    if error:
        raise HTTPException(status_code=503)
    raise HTTPException(status_code=404)

@app.get("/{bucket}/")
async def list_bucket(bucket: str):
    """List all items in a bucket"""
    async with httpx.AsyncClient() as client:
        async def fetch_server(server):
            try:
                result = await client.get(server + bucket + '/', timeout=2)
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
