"""Basic simple bucket async replication"""

import argparse
import warnings
import random
import sys
import requests

TIMEOUT=2048

def find_space(locator, bucket, object_size, current, desired):
    """Find servers with space for replication"""
    # TODO refactor with locator_api.add_object
    res = requests.get(locator + 'health', timeout=4)
    res.raise_for_status()
    health = res.json()['servers']
    candidates = {server: stats['available'] * stats['percent'] for server, stats in health.items()
                  if stats['write'] and stats['percent'] > 1
                  and stats['available'] > object_size + 1024*1024
                  and server not in current}
    for server in candidates.keys():
        try:
            result = requests.head(server + bucket + "/", timeout=1)
            result.raise_for_status()
        except requests.exceptions.RequestException:
            candidates.pop(server)
    if not candidates:
        return []
    # TODO emit warning?
    desired = min(desired, len(candidates))
    return random.choices(list(candidates.keys()), list(candidates.values()), k=desired)

def get_object_size(obj, skip_404=False):
    """HEAD an object to determine its size and checksum"""
    result = requests.head(obj, timeout=2)
    if skip_404 and result.status_code == 404:
        return 0, None
    result.raise_for_status()
    return int(result.headers['Content-Length']), result.headers['Repr-Digest']

def replicate_object(source, dest):
    """Replicate one object"""
    # TODO multiple at once
    size, cksum = get_object_size(source)
    assert size
    assert cksum
    assert not any(get_object_size(dest, skip_404=True))
    with requests.get(source, stream=True, timeout=TIMEOUT) as get:
        get.raise_for_status()
        assert int(get.headers['Content-Length']) == size
        assert get.headers['Repr-Digest'] == cksum
        # TODO Repr-Digest? Content-Length?
        put = requests.put(dest, data=get.raw, timeout=TIMEOUT,
                           headers={#'Content-Length': str(size),
                                    'Content-Digest': cksum})#,
#                                    'Transfer-Encoding': 'identity'})
        put.raise_for_status()
    assert get_object_size(dest) == (size, cksum)
    # TODO return checksum also?
    return size

def get_bucket_contents(bucket):
    """Return each object in a bucket and its size"""
    result = requests.get(bucket, timeout=10)
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
    res = requests.get(locator + bucket + '/', timeout=8)
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
            warnings.warn('No space to replicate object {name}')
            error = True
            continue
        if len(spaces) < desired:
            warnings.warn('Not enough spaces but will still do some...')
            error = True
        for run in spaces:
            src = random.choice(obj['locations']) + bucket + '/' + name
            dst = run +  bucket + '/' + name
            print(f"{src} => {dst}")
            assert replicate_object(src, dst) == obj['size']
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
    #replicate_bucket(args.source, args.dest)
    sys.exit(int(not auto_replica(args.locator, args.bucket, args.replicas)))


if __name__ == '__main__':
    cli()
