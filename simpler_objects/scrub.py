"""Post-crash scrub utility for the object server.

Walks an OBJECT_DIRECTORY-style root, identifies files left behind by a
crashed PUT (no valid checksum entry), and optionally removes them.

Run this **after a crash and before restarting** the object server.
"""

import argparse
import dataclasses
import datetime
import os
import pathlib
import sys
import time
from typing import Iterable, List, Optional, Set

from simpler_objects.common import parse_checksum_line


@dataclasses.dataclass
class BucketReport:
    name: str
    valid_entries: int = 0
    garbled_lines: List[str] = dataclasses.field(default_factory=list)
    stale_entries: List[str] = dataclasses.field(default_factory=list)
    crash_victims: List[pathlib.Path] = dataclasses.field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.garbled_lines or self.stale_entries or self.crash_victims)


def _checksum_path(bucket_dir: pathlib.Path) -> pathlib.Path:
    return bucket_dir.parent / f"{bucket_dir.name}.sha256"


def scan_bucket(bucket_dir: pathlib.Path,
                max_age: Optional[float] = None,
                now: Optional[float] = None) -> BucketReport:
    """Inspect one bucket and return a report.

    A file in the bucket directory with no valid checksum entry is a
    crash victim. Garbled checksum lines and stale entries (valid line,
    file missing) are also collected but require --repair-checksums to
    act on.
    """
    report = BucketReport(name=bucket_dir.name)
    valid_keys: Set[str] = set()
    cksum_path = _checksum_path(bucket_dir)
    if cksum_path.is_file():
        with open(cksum_path, encoding='utf-8') as fp:
            for line in fp:
                if not line.strip():
                    continue
                parsed = parse_checksum_line(line)
                if parsed is None:
                    report.garbled_lines.append(line.rstrip('\n'))
                    continue
                report.valid_entries += 1
                valid_keys.add(parsed[1])

    on_disk = {entry.name for entry in bucket_dir.iterdir()
               if entry.is_file() and not entry.is_symlink()}
    report.stale_entries = sorted(valid_keys - on_disk)

    cutoff: Optional[float] = None
    if max_age is not None:
        if now is None:
            now = time.time()
        cutoff = now - max_age

    for entry in sorted(bucket_dir.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.name in valid_keys:
            continue
        if cutoff is not None and entry.stat().st_mtime < cutoff:
            continue
        report.crash_victims.append(entry)
    return report


def _iter_bucket_dirs(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            yield entry


def _rewrite_checksum_file(bucket_dir: pathlib.Path,
                           keep_filenames: Set[str]) -> None:
    """Atomically rewrite <bucket>.sha256 keeping only valid lines whose
    filename is in keep_filenames.

    Assumes the object server is stopped — no concurrent append protection.
    """
    cksum_path = _checksum_path(bucket_dir)
    if not cksum_path.is_file():
        return
    tmp_path = cksum_path.with_name(f"{cksum_path.name}.new")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', closefd=False) as out:
            with open(cksum_path, encoding='utf-8') as src:
                for line in src:
                    parsed = parse_checksum_line(line)
                    if parsed is None:
                        continue
                    if parsed[1] not in keep_filenames:
                        continue
                    out.write(f"{parsed[0]}  {parsed[1]}\n")
            out.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, cksum_path)


def _format_mtime(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec='seconds')


def _print_report(report: BucketReport) -> None:
    print(f"bucket {report.name}: "
          f"{report.valid_entries} valid, "
          f"{len(report.garbled_lines)} garbled, "
          f"{len(report.stale_entries)} stale, "
          f"{len(report.crash_victims)} crash-victim")
    for line in report.garbled_lines:
        print(f"  garbled-line: {line!r}")
    for name in report.stale_entries:
        print(f"  stale-entry: {name}")
    for path in report.crash_victims:
        st = path.stat()
        print(f"  crash-victim: {path} size={st.st_size} mtime={_format_mtime(st.st_mtime)}")


def scrub_directory(root: pathlib.Path,
                    apply: bool = False,
                    max_age: Optional[float] = None,
                    repair_checksums: bool = False) -> bool:
    """Scan every bucket under root and act on findings.

    Returns True if the scan completed without issues OR every issue was
    successfully acted on (apply=True path); False if any issue remains
    (dry-run with findings, or apply with a delete failure).
    """
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return False

    now = time.time()
    any_issues = False
    any_unhandled = False
    for bucket_dir in _iter_bucket_dirs(root):
        report = scan_bucket(bucket_dir, max_age=max_age, now=now)
        _print_report(report)
        if not report.has_issues:
            continue
        any_issues = True

        if not apply:
            any_unhandled = True
            continue

        for path in report.crash_victims:
            try:
                path.unlink()
                print(f"  removed: {path}")
            except OSError as e:
                print(f"  failed to remove {path}: {e}", file=sys.stderr)
                any_unhandled = True

        if repair_checksums and (report.garbled_lines or report.stale_entries):
            on_disk = {entry.name for entry in bucket_dir.iterdir()
                       if entry.is_file() and not entry.is_symlink()}
            try:
                _rewrite_checksum_file(bucket_dir, on_disk)
                print(f"  repaired: {_checksum_path(bucket_dir)}")
            except OSError as e:
                print(f"  failed to repair {_checksum_path(bucket_dir)}: {e}",
                      file=sys.stderr)
                any_unhandled = True
        elif report.garbled_lines or report.stale_entries:
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
    parser.add_argument("--apply", action="store_true",
                        help="Actually unlink crash-victim files "
                             "(default: dry-run report only)")
    parser.add_argument("--max-age", type=float, default=None,
                        metavar="SECONDS",
                        help="Only inspect files modified within the last "
                             "N seconds (likely crash victims). Default: "
                             "inspect every file.")
    parser.add_argument("--repair-checksums", action="store_true",
                        help="Atomically rewrite each <bucket>.sha256 "
                             "without garbled or stale lines. Requires "
                             "--apply.")
    args = parser.parse_args()
    clean = scrub_directory(
        pathlib.Path(args.directory),
        apply=args.apply,
        max_age=args.max_age,
        repair_checksums=args.repair_checksums,
    )
    sys.exit(0 if clean else 1)


if __name__ == '__main__':
    cli()
