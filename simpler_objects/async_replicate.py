"""Basic simple bucket async replication"""

import argparse
import requests

TIMEOUT=2048

def get_object_size(obj, skip_404=False):
    """HEAD an object to determine its size"""
    result = requests.head(obj, timeout=2)
    if skip_404 and result.status_code == 404:
        return 0, None
    result.raise_for_status()
    return int(result.headers['Content-Length']), result.headers['Repr-Digest']

def replicate_object(source, dest):
    """Replicate one object"""
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
    source_contents = get_bucket_contents(source)
    dest_contents = get_bucket_contents(dest)
    # NOTE this "size" is a tuple that includes also a sha256
    for obj, size in source_contents.items():
        if obj in dest_contents:
            assert size == dest_contents[obj]
            continue
        # TODO check checksum also? (need to convert it)
        assert replicate_object(source + obj, dest + obj) == size[0]

def cli():
    """CLI"""
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("dest")
    args = parser.parse_args()
    replicate_bucket(args.source, args.dest)


if __name__ == '__main__':
    cli()
