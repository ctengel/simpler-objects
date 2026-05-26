"""Post-crash scrub utility for the object server.

Walks an OBJECT_DIRECTORY-style root, identifies files left behind by a
crashed PUT (no valid checksum entry), and optionally removes them.

Run this **after a crash and before restarting** the object server.
"""

import argparse
import datetime
import os
import pathlib
import sys
from typing import Set

from simpler_objects.common import ChecksumFile, parse_checksum_line


def scan_bucket(bucket_dir: pathlib.Path):
    """Inspect one bucket.

    Returns (crash_victims, garbled_lines, stale_entries):
      crash_victims — paths with no valid checksum entry
      garbled_lines — unparseable lines in <bucket>.sha256
      stale_entries — valid checksum lines whose file is missing
    """
    valid_keys: Set[str] = set()
    garbled_lines = []
    cksum_path = ChecksumFile(bucket_dir).path
    if cksum_path.is_file():
        with open(cksum_path, encoding='utf-8') as fp:
            for line in fp:
                if not line.strip():
                    continue
                parsed = parse_checksum_line(line)
                if parsed is None:
                    garbled_lines.append(line.rstrip('\n'))
                else:
                    valid_keys.add(parsed[1])

    on_disk = {e.name for e in bucket_dir.iterdir()
               if e.is_file() and not e.is_symlink()}
    stale_entries = sorted(valid_keys - on_disk)
    crash_victims = [bucket_dir / name for name in sorted(on_disk - valid_keys)]
    return crash_victims, garbled_lines, stale_entries


def _rewrite_checksum_file(bucket_dir: pathlib.Path,
                           keep_filenames: Set[str]) -> None:
    """Atomically rewrite <bucket>.sha256 keeping only valid lines whose
    filename is in keep_filenames.

    Assumes the object server is stopped — no concurrent append protection.
    """
    cksum = ChecksumFile(bucket_dir)
    if not cksum.path.is_file():
        return
    tmp_path = cksum.path.with_name(f"{cksum.path.name}.new")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', closefd=False) as out:
            for digest, filename in cksum:
                if filename in keep_filenames:
                    out.write(f"{digest}  {filename}\n")
            out.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, cksum.path)


def scrub_directory(root: pathlib.Path,
                    delete_victims: bool = False,
                    repair_checksums: bool = False) -> bool:
    """Scan every bucket under root and act on findings.

    Returns True if the scan completed without issues OR every issue was
    successfully acted on; False if any issue remains (dry-run with findings,
    or an action that failed).
    """
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return False

    any_issues = False
    any_unhandled = False
    for bucket_dir in sorted(root.iterdir()):
        if not bucket_dir.is_dir() or bucket_dir.is_symlink():
            continue
        crash_victims, garbled_lines, stale_entries = scan_bucket(bucket_dir)

        print(f"bucket {bucket_dir.name}: "
              f"{len(garbled_lines)} garbled, "
              f"{len(stale_entries)} stale, "
              f"{len(crash_victims)} crash-victim")
        for line in garbled_lines:
            print(f"  garbled-line: {line!r}")
        for name in stale_entries:
            print(f"  stale-entry: {name}")
        for path in crash_victims:
            st = path.stat()
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')
            print(f"  crash-victim: {path} size={st.st_size} mtime={mtime}")

        if not any([crash_victims, garbled_lines, stale_entries]):
            continue
        any_issues = True

        for path in crash_victims:
            if delete_victims:
                try:
                    path.unlink()
                    print(f"  removed: {path}")
                except OSError as e:
                    print(f"  failed to remove {path}: {e}", file=sys.stderr)
                    any_unhandled = True
            else:
                any_unhandled = True

        if garbled_lines or stale_entries:
            if repair_checksums:
                on_disk = {e.name for e in bucket_dir.iterdir()
                           if e.is_file() and not e.is_symlink()}
                try:
                    _rewrite_checksum_file(bucket_dir, on_disk)
                    print(f"  repaired: {ChecksumFile(bucket_dir).path}")
                except OSError as e:
                    print(f"  failed to repair {ChecksumFile(bucket_dir).path}: {e}",
                          file=sys.stderr)
                    any_unhandled = True
            else:
                any_unhandled = True

    if not any_issues:
        print("scrub: no issues found")
        return True
    return not any_unhandled


def cli():
    """CLI"""
    parser = argparse.ArgumentParser(
        description=("Post-crash scrub utility for the object server. "
                     "Walks each bucket, identifies files with no valid "
                     "checksum entry (crash victims), and reports or "
                     "removes them. Run after a crash, before restarting "
                     "the object server."),
    )
    parser.add_argument("directory",
                        help="OBJECT_DIRECTORY to scrub")
    parser.add_argument("--delete-victims", action="store_true",
                        help="Unlink crash-victim files "
                             "(default: dry-run report only)")
    parser.add_argument("--repair-checksums", action="store_true",
                        help="Atomically rewrite each <bucket>.sha256 "
                             "without garbled or stale lines "
                             "(default: dry-run report only)")
    args = parser.parse_args()
    clean = scrub_directory(
        pathlib.Path(args.directory),
        delete_victims=args.delete_victims,
        repair_checksums=args.repair_checksums,
    )
    sys.exit(0 if clean else 1)


if __name__ == '__main__':
    cli()
