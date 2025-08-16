"""Simpler Objects Locator API"""

import os
import random
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

app = FastAPI()

OBJECT_SERVERS = os.environ.get('OBJECT_SERVERS', 'http://localhost:46579/')

def object_servers(randomized=False):
    """Return a randomized list of object server URLs"""
    servers = OBJECT_SERVERS.split(',')
    if randomized:
        random.shuffle(servers)
    return servers

@app.get("/{object_path}")
def find_object(object_path: str):
    """Return a redirect to an existing object"""
    for server in object_servers(randomized=True):
        try:
            result = requests.head(server + object_path, timeout=1)
            result.raise_for_status()
        except requests.exceptions.RequestException:
            continue
        return RedirectResponse(url=server+object_path)
    raise HTTPException(status_code=404)

@app.put("/{object_path}")
def add_object(object_path: str):
    """Return a redirect to a server that can handle an object request"""
    server_to_upload = None
    for server in object_servers(randomized=True):
        try:
            result = requests.head(server + object_path, timeout=1)
        except requests.exceptions.RequestException:
            continue
        if result.status_code != 404:
            raise HTTPException(status_code=409)
        if not server_to_upload:
            server_to_upload = server
    return RedirectResponse(url=server_to_upload+object_path)
