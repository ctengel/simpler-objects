"""Basic simple bucket async replication"""

import argparse
import logging
import random
import sys
import httpx
from simpler_objects.common import filter_write_candidates
from simpler_objects.logging_config import configure

# Explicit name (rather than __name__) so the logger label stays stable when
# the module is run as __main__ via `python -m simpler_objects.async_replicate`.
logger = logging.getLogger("simpler_objects.async_replicate")

TIMEOUT=2048

def find_space(locator, bucket, object_size, current, desired):
    """Find servers with space for replication"""
    res = httpx.get(locator + 'health', timeout=4)
    res.raise_for_status()
    health = res.json()['servers']
    candidates = filter_write_candidates(health, object_size, exclude=current)
    for server in list(candidates.keys()):
        try:
            result = httpx.head(server + bucket + "/", timeout=1)
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
    result = httpx.head(obj, timeout=2)
    if skip_404 and result.status_code == 404:
        return 0, None
    result.raise_for_status()
    return int(result.headers['content-length']), result.headers.get('repr-digest')

def replicate_object(source, dest):
    """Replicate one object"""
    # TODO multiple at once
    size, cksum = get_object_size(source)
    assert size
    assert cksum
    assert not any(get_object_size(dest, skip_404=True))
    with httpx.stream("GET", source, timeout=TIMEOUT) as get:
        get.raise_for_status()
        assert int(get.headers['content-length']) == size
        assert get.headers['repr-digest'] == cksum
        put = httpx.put(dest, content=get.iter_bytes(),
                        headers={'Content-Length': str(size),
                                 'Content-Digest': cksum},
                        timeout=TIMEOUT)
        put.raise_for_status()
    assert get_object_size(dest) == (size, cksum)
    # TODO return checksum also?
    return size

def get_bucket_contents(bucket):
    """Return each object in a bucket and its size"""
    result = httpx.get(bucket, timeout=10)
    result.raise_for_status()
    return {k: (v['size'], v['checksum'])
            for k, v in result.json()["objects"].items()
            if not v['directory']}

def replicate_bucket(source, dest):
    """Replicate any missing objects"""
    # TODO do we still want a CLI way to invoke this?
    source_contents = get_bucket_contents(source)
    dest_contents = get_bucket_contents(dest)
    # NOTE this "size" is a tuple that includes also a sha256
    for obj, size in source_contents.items():
        if obj in dest_contents:
            assert size == dest_contents[obj]
            continue
        # TODO check checksum also? (need to convert it)
        assert replicate_object(source + obj, dest + obj) == size[0]

def auto_replica(locator, bucket, replicas):
    """Just figure out where to put stuff and do it"""
    res = httpx.get(locator + bucket + '/', timeout=8)
    res.raise_for_status()
    contents = res.json()
    error = False
    for name, obj in contents['objects'].items():
        if obj['error'] or not obj['checksum']:
            logger.warning("replicate.skip", extra={
                "bucket": bucket, "key": name,
                "reason": "listing_error" if obj['error'] else "missing_checksum",
            })
            error = True
            continue
        desired = replicas - len(obj['locations'])
        if desired < 1:
            continue
        spaces = find_space(locator, bucket, obj['size'], obj['locations'], desired)
        if not spaces:
            logger.warning("replicate.no_space", extra={
                "bucket": bucket, "key": name,
                "current_locations": obj['locations'],
                "desired_extra": desired,
            })
            error = True
            continue
        if len(spaces) < desired:
            logger.warning("replicate.partial", extra={
                "bucket": bucket, "key": name,
                "got": len(spaces), "wanted": desired,
            })
            error = True
        for run in spaces:
            src = random.choice(obj['locations']) + bucket + '/' + name
            dst = run +  bucket + '/' + name
            logger.info("replicate.copy", extra={
                "bucket": bucket, "key": name, "src": src, "dst": dst,
            })
            copied = replicate_object(src, dst)
            assert copied == obj['size']
            logger.info("replicate.copy.done", extra={
                "bucket": bucket, "key": name, "src": src, "dst": dst,
                "size": copied,
            })
    return not error


def cli():
    """CLI"""
    parser = argparse.ArgumentParser()
    #parser.add_argument("source")
    #parser.add_argument("dest")
    parser.add_argument("locator")
    parser.add_argument("bucket")
    parser.add_argument("replicas", type=int)
    args = parser.parse_args()
    configure()
    #replicate_bucket(args.source, args.dest)
    sys.exit(int(not auto_replica(args.locator, args.bucket, args.replicas)))


if __name__ == '__main__':
    cli()
