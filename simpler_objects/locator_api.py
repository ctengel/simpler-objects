"""Simpler Objects Locator API"""

import os
import random
from typing import Annotated
import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import RedirectResponse

app = FastAPI()

OBJECT_SERVERS = os.environ.get('OBJECT_SERVERS', 'http://localhost:46579/')

def object_servers(randomized=False):
    """Return a randomized list of object server URLs"""
    servers = OBJECT_SERVERS.split(',')
    if randomized:
        random.shuffle(servers)
    return servers

def get_object_server_health(url: str):
    """Get the health of an object server"""
    try:
        result = requests.get(url + 'health', timeout=1)
        result.raise_for_status()
    except requests.exceptions.RequestException:
        return {'write': False, 'read': False, 'available': 0, 'percent': 0}
    return result.json()

@app.get("/{bucket}/{key}")
def find_object(bucket: str, key: str):
    """Return a redirect to an existing object"""
    object_path = f"{bucket}/{key}"
    for server in object_servers(randomized=True):
        try:
            result = requests.head(server + object_path, timeout=1)
            result.raise_for_status()
        except requests.exceptions.RequestException:
            continue
        return RedirectResponse(url=server+object_path)
    raise HTTPException(status_code=404)

@app.put("/{bucket}/{key}")
def add_object(bucket: str, key: str, content_length: Annotated[int | None, Header()] = None):
    """Return a redirect to a server that can handle an object request"""
    assert content_length
    object_path = f"{bucket}/{key}"
    object_size = content_length
    # TODO use caches of objects and servers but then double check vs checking everybody
    all_obj_servers = object_servers()
    health = {server: get_object_server_health(server) for server in all_obj_servers}
    candidates = {server: stats['available'] * stats['percent'] for server, stats in health.items()
                  if stats['write'] and stats['percent'] > 1
                  and stats['available'] > object_size + 1024*1024}
    for server in all_obj_servers:
        try:
            result = requests.head(server + object_path, timeout=1)
        except requests.exceptions.RequestException:
            candidates.pop(server, None)
        if result.status_code != 404:
            raise HTTPException(status_code=409)
    for server in candidates.keys():
        try:
            result = requests.head(server + bucket, timeout=1)
            result.raise_for_status()
        except requests.exceptions.RequestException:
            candidates.pop(server)
    if not candidates:
        raise HTTPException(507)
    server_to_upload = random.choices(list(candidates.keys()), list(candidates.values()))[0]
    return RedirectResponse(url=server_to_upload+object_path)
