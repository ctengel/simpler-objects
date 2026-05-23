"""Tests for the post-crash scrub utility (simpler_objects.scrub)."""

import hashlib
import os
import pathlib
import time

import pytest

from simpler_objects import scrub


BUCKET = "test-bucket"
OTHER_BUCKET = "other-bucket"


def _hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_object(root: pathlib.Path, bucket: str, key: str, content: bytes,
                  with_checksum: bool = True) -> pathlib.Path:
    bucket_dir = root / bucket
    bucket_dir.mkdir(exist_ok=True)
    path = bucket_dir / key
    path.write_bytes(content)
    if with_checksum:
        cksum = root / f"{bucket}.sha256"
        with open(cksum, "a", encoding="utf-8") as fp:
            fp.write(f"{_hex(content)}  {key}\n")
    return path


@pytest.fixture()
def root(tmp_path):
    (tmp_path / BUCKET).mkdir()
    return tmp_path


# --- scan_bucket ---

def test_scan_clean_bucket(root):
    _write_object(root, BUCKET, "a.bin", b"hello")
    _write_object(root, BUCKET, "b.bin", b"world")
    report = scrub.scan_bucket(root / BUCKET)
    assert report.valid_entries == 2
    assert report.garbled_lines == []
    assert report.stale_entries == []
    assert report.crash_victims == []
    assert not report.has_issues


def test_scan_orphan_file_is_crash_victim(root):
    _write_object(root, BUCKET, "good.bin", b"committed")
    orphan = root / BUCKET / "orphan.bin"
    orphan.write_bytes(b"partial")
    report = scrub.scan_bucket(root / BUCKET)
    assert report.valid_entries == 1
    assert [p.name for p in report.crash_victims] == ["orphan.bin"]


def test_scan_garbled_lines(root):
    _write_object(root, BUCKET, "good.bin", b"committed")
    cksum = root / f"{BUCKET}.sha256"
    with open(cksum, "a", encoding="utf-8") as fp:
        fp.write("torn-single-field\n")
        fp.write("notenoughhex  also-bad.bin\n")
    report = scrub.scan_bucket(root / BUCKET)
    assert report.valid_entries == 1
    assert len(report.garbled_lines) == 2


def test_scan_object_with_only_garbled_entry_is_crash_victim(root):
    """A file whose only checksum line is garbled has no valid entry."""
    bucket_dir = root / BUCKET
    (bucket_dir / "torn.bin").write_bytes(b"data")
    cksum = root / f"{BUCKET}.sha256"
    # Absorbed-append: the digest field has trailing junk → 70 chars, not 64
    cksum.write_text(f"{'a' * 70}  torn.bin\n")
    report = scrub.scan_bucket(bucket_dir)
    assert report.valid_entries == 0
    assert len(report.garbled_lines) == 1
    assert [p.name for p in report.crash_victims] == ["torn.bin"]


def test_scan_stale_entry_reported(root):
    """Valid checksum line for a missing file → stale entry."""
    bucket_dir = root / BUCKET
    cksum = root / f"{BUCKET}.sha256"
    cksum.write_text(f"{'a' * 64}  ghost.bin\n")
    report = scrub.scan_bucket(bucket_dir)
    assert report.valid_entries == 1
    assert report.stale_entries == ["ghost.bin"]
    assert report.crash_victims == []


def test_max_age_excludes_old_files(root):
    bucket_dir = root / BUCKET
    young = bucket_dir / "young.bin"
    young.write_bytes(b"recent")
    old = bucket_dir / "old.bin"
    old.write_bytes(b"ancient")
    now = time.time()
    os.utime(old, (now - 86400, now - 86400))  # 1 day old
    os.utime(young, (now - 10, now - 10))      # 10 seconds old
    report = scrub.scan_bucket(bucket_dir, max_age=3600, now=now)
    assert [p.name for p in report.crash_victims] == ["young.bin"]


def test_scan_skips_symlinks(root):
    bucket_dir = root / BUCKET
    target = root / "external.bin"
    target.write_bytes(b"x")
    (bucket_dir / "link.bin").symlink_to(target)
    report = scrub.scan_bucket(bucket_dir)
    assert report.crash_victims == []


def test_scan_skips_subdirectories(root):
    bucket_dir = root / BUCKET
    (bucket_dir / "subdir").mkdir()
    report = scrub.scan_bucket(bucket_dir)
    assert report.crash_victims == []


# --- scrub_directory dry-run ---

def test_dry_run_leaves_filesystem_untouched(root, capsys):
    orphan = root / BUCKET / "orphan.bin"
    orphan.write_bytes(b"partial")
    cksum = root / f"{BUCKET}.sha256"
    cksum.write_text("garbled\n")
    clean = scrub.scrub_directory(root, apply=False)
    assert clean is False
    assert orphan.exists()
    assert cksum.read_text() == "garbled\n"


def test_clean_run_returns_true(root, capsys):
    _write_object(root, BUCKET, "a.bin", b"hello")
    clean = scrub.scrub_directory(root, apply=False)
    assert clean is True


# --- scrub_directory --apply ---

def test_apply_removes_orphan(root):
    orphan = root / BUCKET / "orphan.bin"
    orphan.write_bytes(b"partial")
    clean = scrub.scrub_directory(root, apply=True)
    assert clean is True
    assert not orphan.exists()


def test_apply_without_repair_leaves_garbled_lines(root):
    cksum = root / f"{BUCKET}.sha256"
    cksum.write_text("torn-fragment\n")
    clean = scrub.scrub_directory(root, apply=True)
    assert clean is False
    assert cksum.read_text() == "torn-fragment\n"


def test_repair_strips_garbled_lines(root):
    _write_object(root, BUCKET, "good.bin", b"committed")
    cksum = root / f"{BUCKET}.sha256"
    with open(cksum, "a", encoding="utf-8") as fp:
        fp.write("torn-fragment\n")
        fp.write(f"{'a' * 70}  bogus.bin\n")
    clean = scrub.scrub_directory(root, apply=True, repair_checksums=True)
    assert clean is True
    text = cksum.read_text()
    assert "torn-fragment" not in text
    assert "bogus.bin" not in text
    assert f"{_hex(b'committed')}  good.bin" in text


def test_repair_strips_stale_entries(root):
    """Valid line pointing to a deleted file is removed by --repair-checksums."""
    cksum = root / f"{BUCKET}.sha256"
    cksum.write_text(f"{'a' * 64}  ghost.bin\n")
    clean = scrub.scrub_directory(root, apply=True, repair_checksums=True)
    assert clean is True
    assert cksum.read_text() == ""


def test_mixed_bucket_only_orphan_is_removed(root):
    good_path = _write_object(root, BUCKET, "good.bin", b"committed")
    orphan = root / BUCKET / "orphan.bin"
    orphan.write_bytes(b"partial")
    cksum = root / f"{BUCKET}.sha256"
    with open(cksum, "a", encoding="utf-8") as fp:
        fp.write("torn\n")
    clean = scrub.scrub_directory(root, apply=True, repair_checksums=True)
    assert clean is True
    assert good_path.exists()
    assert not orphan.exists()
    text = cksum.read_text()
    assert "torn" not in text
    assert "good.bin" in text


def test_multiple_buckets_scanned(root):
    (root / OTHER_BUCKET).mkdir()
    _write_object(root, BUCKET, "good.bin", b"x")
    orphan_a = root / BUCKET / "orphan_a.bin"
    orphan_a.write_bytes(b"p1")
    orphan_b = root / OTHER_BUCKET / "orphan_b.bin"
    orphan_b.write_bytes(b"p2")
    clean = scrub.scrub_directory(root, apply=True)
    assert clean is True
    assert not orphan_a.exists()
    assert not orphan_b.exists()


def test_max_age_via_scrub_directory(root):
    bucket_dir = root / BUCKET
    young = bucket_dir / "young.bin"
    young.write_bytes(b"recent")
    old = bucket_dir / "old.bin"
    old.write_bytes(b"ancient")
    now = time.time()
    os.utime(old, (now - 86400, now - 86400))
    # Only inspect files modified in the last hour → only young is a victim
    clean = scrub.scrub_directory(root, apply=True, max_age=3600)
    assert clean is True
    assert not young.exists()
    assert old.exists()


def test_missing_directory_returns_false(tmp_path, capsys):
    clean = scrub.scrub_directory(tmp_path / "does-not-exist", apply=False)
    assert clean is False


# --- repair atomicity & format ---

def test_repair_preserves_valid_line_format(root):
    """After repair, surviving lines round-trip through parse_checksum_line."""
    from simpler_objects.common import parse_checksum_line
    _write_object(root, BUCKET, "a.bin", b"alpha")
    _write_object(root, BUCKET, "b.bin", b"beta")
    cksum = root / f"{BUCKET}.sha256"
    with open(cksum, "a", encoding="utf-8") as fp:
        fp.write("garbled-line\n")
    scrub.scrub_directory(root, apply=True, repair_checksums=True)
    lines = cksum.read_text().splitlines()
    parsed = [parse_checksum_line(line + "\n") for line in lines]
    assert all(p is not None for p in parsed)
    assert sorted(p[1] for p in parsed) == ["a.bin", "b.bin"]


def test_repair_skipped_when_no_dirty_lines(root):
    """Clean .sha256 is not rewritten if there's nothing to repair."""
    _write_object(root, BUCKET, "a.bin", b"alpha")
    cksum = root / f"{BUCKET}.sha256"
    mtime_before = cksum.stat().st_mtime
    time.sleep(0.01)
    scrub.scrub_directory(root, apply=True, repair_checksums=True)
    # No issues, no rewrite — mtime unchanged.
    assert cksum.stat().st_mtime == mtime_before
