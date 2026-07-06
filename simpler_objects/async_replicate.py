"""Basic simple bucket async replication"""

import argparse
import os
import urllib.parse
import warnings
import random
import sys
import httpx
from simpler_objects import auth
from simpler_objects.common import filter_write_candidates

TIMEOUT=2048

# Shared HMAC secret to self-sign direct object-server requests; the
# replicator is cluster-internal, so it holds the same secret the locator
# uses rather than a client API key. Unset = no signing.
CLUSTER_SECRET = os.environ.get('CLUSTER_SECRET', '')
# API key for the locator-facing requests (bucket listing); unset = none.
API_KEY = os.environ.get('API_KEY', '')
# PEM bundle to verify HTTPS servers against; unset = system trust store.
CA_BUNDLE = os.environ.get('CA_BUNDLE', '')

def signed_suffix(operation, bucket, key=""):
    """Signed-query suffix for a direct object-server URL ('' when disabled)."""
    if not CLUSTER_SECRET:
        return ""
    return "?" + auth.signed_query(CLUSTER_SECRET, operation, bucket, key)

def locator_headers():
    """Authorization header for locator requests, when an API key is set."""
    return {'Authorization': f'Bearer {API_KEY}'} if API_KEY else {}

def tls_verify():
    """Value for httpx verify= — the CA bundle path, or default trust."""
    return CA_BUNDLE or True

def find_space(locator, bucket, object_size, current, desired):
    """Find servers with space for replication"""
    res = httpx.get(locator + 'health', timeout=4, verify=tls_verify())
    res.raise_for_status()
    health = res.json()['servers']
    candidates = filter_write_candidates(health, object_size, exclude=current)
    for server in list(candidates.keys()):
        try:
            result = httpx.head(server + bucket + "/" + signed_suffix(auth.OP_LIST, bucket),
                                timeout=1, verify=tls_verify())
            result.raise_for_status()
        except httpx.HTTPError:
            candidates.pop(server)
    if not candidates:
        return []
    # TODO emit warning?
    desired = min(desired, len(candidates))
    return random.choices(list(candidates.keys()), list(candidates.values()), k=desired)

def get_object_size(obj, skip_404=False):
    """HEAD an object to determine its size and checksum"""
    result = httpx.head(obj, timeout=2, verify=tls_verify())
    if skip_404 and result.status_code == 404:
        return 0, None
    result.raise_for_status()
    return int(result.headers['content-length']), result.headers.get('repr-digest')

def replicate_object(source, dest, bucket="", key=""):
    """Replicate one object.

    ``source``/``dest`` are bare object URLs; ``bucket``/``key`` are needed to
    sign the requests when CLUSTER_SECRET is set (the same URL is HEADed as
    'read' and PUT as 'write', so it cannot be pre-signed by the caller).
    """
    # TODO multiple at once
    read_sfx = signed_suffix(auth.OP_READ, bucket, key)
    size, cksum = get_object_size(source + read_sfx)
    assert size
    assert cksum
    assert not any(get_object_size(dest + read_sfx, skip_404=True))
    with httpx.stream("GET", source + read_sfx, timeout=TIMEOUT, verify=tls_verify()) as get:
        get.raise_for_status()
        assert int(get.headers['content-length']) == size
        assert get.headers['repr-digest'] == cksum
        put = httpx.put(dest + signed_suffix(auth.OP_WRITE, bucket, key),
                        content=get.iter_bytes(),
                        headers={'Content-Length': str(size),
                                 'Content-Digest': cksum},
                        timeout=TIMEOUT, verify=tls_verify())
        put.raise_for_status()
    assert get_object_size(dest + read_sfx) == (size, cksum)
    # TODO return checksum also?
    return size

def get_bucket_contents(bucket):
    """Return each object in a bucket and its size"""
    result = httpx.get(bucket, timeout=16, verify=tls_verify())
    result.raise_for_status()
    return {k: (v['size'], v['checksum'])
            for k, v in result.json()["objects"].items()
            if not v['directory']}

def replicate_bucket(source, dest):
    """Replicate any missing objects"""
    # TODO do we still want a CLI way to invoke this?
    # source/dest are object-server bucket URLs ending "{bucket}/"; recover
    # the bucket name so the per-object requests can be signed.
    bucket = urllib.parse.urlsplit(source).path.strip('/').rsplit('/', 1)[-1]
    list_sfx = signed_suffix(auth.OP_LIST, bucket)
    source_contents = get_bucket_contents(source + list_sfx)
    dest_contents = get_bucket_contents(dest + list_sfx)
    # NOTE this "size" is a tuple that includes also a sha256
    for obj, size in source_contents.items():
        if obj in dest_contents:
            assert size == dest_contents[obj]
            continue
        # TODO check checksum also? (need to convert it)
        assert replicate_object(source + obj, dest + obj, bucket, obj) == size[0]

def auto_replica(locator, bucket, replicas):
    """Just figure out where to put stuff and do it"""
    res = httpx.get(locator + bucket + '/', timeout=32,
                    headers=locator_headers(), verify=tls_verify())
    res.raise_for_status()
    contents = res.json()
    error = False
    for name, obj in contents['objects'].items():
        if obj['error'] or not obj['checksum']:
            warnings.warn(f'Object {name} has an issue.')
            error = True
            continue
        desired = replicas - len(obj['locations'])
        if desired < 1:
            continue
        spaces = find_space(locator, bucket, obj['size'], obj['locations'], desired)
        if not spaces:
            warnings.warn(f'No space to replicate object {name}')
            error = True
            continue
        if len(spaces) < desired:
            warnings.warn('Not enough spaces but will still do some...')
            error = True
        for run in spaces:
            src = random.choice(obj['locations']) + bucket + '/' + name
            dst = run +  bucket + '/' + name
            print(f"{src} => {dst}")
            assert replicate_object(src, dst, bucket, name) == obj['size']
    return not error


def cli():
    """CLI"""
    parser = argparse.ArgumentParser()
    parser.add_argument("locator")
    parser.add_argument("buckets", nargs="*")
    parser.add_argument("--replicas", type=int)
    args = parser.parse_args()

    buckets = args.buckets or os.environ.get("BUCKETS", "").split()
    if not buckets:
        parser.error("specify at least one bucket or set BUCKETS env var")

    if args.replicas is not None:
        results = [auto_replica(args.locator, b, args.replicas) for b in buckets]
    else:
        default_replicas = int(os.environ.get("REPLICAS", "2"))
        results = [
            auto_replica(args.locator, b,
                         int(os.environ.get(f"REPLICAS_{b.upper().replace('-', '_')}", default_replicas)))
            for b in buckets
        ]
    sys.exit(int(not all(results)))


if __name__ == '__main__':
    cli()
