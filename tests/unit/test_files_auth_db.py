"""File identity, upload safety, disk checks, auth primitives and the schema."""

import os
import sqlite3
import time

import pytest
from sqlalchemy import inspect

from app.config import Settings
from app.database import Database
from app.models import Base
from app.services.auth import UserStore, hash_password, make_token, read_token, verify_password
from app.services.file_manager import (Library, LibraryError, UploadReservations, describe_change,
                                       fingerprint, quick_hash, sanitize_upload_name)

EXT = Settings(_env_file=None).allowed_extensions


# ---------------------------------------------------------------- filenames
@pytest.mark.parametrize("raw,expected", [
    ("clip.mp4", "clip.mp4"),
    ("Store A cam 3.MP4", "Store A cam 3.MP4"),
    ("../../etc/passwd.mp4", "passwd.mp4"),
    ("C:\\Users\\x\\video.mkv", "video.mkv"),
    # parentheses are harmless (argv is never passed through a shell); ; $ are replaced
    ("a;rm -rf $(x).mp4", "a_rm -rf _(x).mp4"),
    ("vidéo.mov", "vid_o.mov"),
    (".hidden.mp4", "hidden.mp4"),
])
def test_sanitize_upload_name(raw, expected):
    out = sanitize_upload_name(raw, EXT)
    assert "/" not in out and "\\" not in out and not out.startswith(".")
    assert out == expected


@pytest.mark.parametrize("bad", ["", "..", ".mp4", "noext", "x.exe", "x.mp4.sh", "x.txt"])
def test_sanitize_rejects(bad):
    with pytest.raises(LibraryError):
        sanitize_upload_name(bad, EXT)


def test_library_resolve_blocks_traversal(tmp_path):
    s = Settings(_env_file=None, video_dir=tmp_path / "v")
    (tmp_path / "v").mkdir()
    lib = Library(s)
    assert lib.resolve("a/b.mp4") == (tmp_path / "v" / "a" / "b.mp4").resolve()
    for bad in ("../x.mp4", "/etc/passwd", "a/../../x", "", "a\x00b"):
        with pytest.raises(LibraryError):
            lib.resolve(bad)


def test_library_scan_skips_partials_hidden_and_symlinks(tmp_path):
    vd = tmp_path / "v"
    (vd / "sub").mkdir(parents=True)
    (vd / "a.mp4").write_bytes(b"x")
    (vd / "sub" / "b.mkv").write_bytes(b"x")
    (vd / "c.mp4.part").write_bytes(b"x")
    (vd / ".hidden.mp4").write_bytes(b"x")
    (vd / "notes.txt").write_bytes(b"x")
    (vd / ".uploads").mkdir()
    (vd / ".uploads" / "d.mp4").write_bytes(b"x")
    os.symlink("/etc/hosts", vd / "link.mp4")
    lib = Library(Settings(_env_file=None, video_dir=vd))
    assert sorted(r for r, _ in lib.iter_files()) == ["a.mp4", "sub/b.mkv"]


# ---------------------------------------------------------------- identity
def test_fingerprint_and_change_detection(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(os.urandom(3 * 1024 * 1024))
    fp = fingerprint(f)
    assert describe_change(fp, f) is None
    assert fp["quick_hash"] == quick_hash(f)

    # modified in place (same inode, size unchanged, new mtime)
    with f.open("r+b") as fh:
        fh.write(b"zz")
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    assert "modified" in describe_change(fp, f)

    # truncated
    fp = fingerprint(f)
    with f.open("r+b") as fh:
        fh.truncate(1000)
    assert "size changed" in describe_change(fp, f)

    # replaced by a different file (new inode)
    fp = fingerprint(f)
    g = tmp_path / "new.mp4"
    g.write_bytes(b"other")
    os.replace(g, f)
    assert "replaced" in describe_change(fp, f)

    # deleted
    fp = fingerprint(f)
    f.unlink()
    assert "deleted" in describe_change(fp, f)


def test_quick_hash_detects_content_change_same_size(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"a" * 5000)
    h1 = quick_hash(f)
    f.write_bytes(b"b" * 5000)
    assert quick_hash(f) != h1


# ---------------------------------------------------------------- disk space
class FakeLib:
    def __init__(self, free, total=100 * 1024**3):
        self.free, self.total = free, total

    def disk_usage(self):
        return {"path": "/x", "total": self.total, "used": self.total - self.free, "free": self.free}

    def reserve_bytes(self, total):
        return 5 * 1024**3


def test_upload_reservation_refuses_when_insufficient():
    r = UploadReservations()
    lib = FakeLib(free=10 * 1024**3)
    with pytest.raises(LibraryError) as e:
        r.reserve(lib, 6 * 1024**3, "big.mp4")  # 10 free - 5 reserve = 5 available
    assert e.value.status_code == 507
    d = e.value.details
    assert d["required"] == 6 * 1024**3 and d["free"] == 10 * 1024**3 and d["available"] == 5 * 1024**3


def test_concurrent_uploads_are_accounted():
    r = UploadReservations()
    lib = FakeLib(free=10 * 1024**3)
    t1 = r.reserve(lib, 3 * 1024**3, "a.mp4")
    with pytest.raises(LibraryError):
        r.reserve(lib, 3 * 1024**3, "b.mp4")  # only 2 GiB left after the first reservation
    with pytest.raises(LibraryError) as e:
        r.reserve(lib, 1, "a.mp4")  # same target name in flight
    assert e.value.status_code == 409
    r.release(t1)
    r.reserve(lib, 3 * 1024**3, "b.mp4")
    assert r.check(lib, 1)["in_flight_uploads"] == 3 * 1024**3


# ---------------------------------------------------------------- auth
def test_password_hashing():
    h = hash_password("s3cret", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "s3cret" not in h
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage")


def test_tokens():
    t = make_token(b"k", "alice", 1)
    assert read_token(b"k", t) == "alice"
    assert read_token(b"other", t) is None
    assert read_token(b"k", t[:-2] + "xx") is None
    assert read_token(b"k", make_token(b"k", "alice", -1)) is None  # expired
    assert read_token(b"k", "nonsense") is None


def test_user_store(tmp_path):
    us = UserStore(tmp_path / "users")
    us.set_user("alice", "operator", "password-1")
    us.set_user("bob", "viewer", "password-2")
    assert oct((tmp_path / "users").stat().st_mode & 0o777) == "0o600"
    assert "password-1" not in (tmp_path / "users").read_text()
    assert us.authenticate("alice", "password-1").can_write
    assert not us.authenticate("bob", "password-2").can_write
    assert us.authenticate("alice", "nope") is None
    assert us.authenticate("nobody", "x") is None
    with pytest.raises(ValueError):
        us.set_user("x:y", "operator", "password-x")
    with pytest.raises(ValueError):
        us.set_user("carol", "operator", "short")  # < 8 characters
    us.set_user("alice", "admin", None)  # role change keeps the password
    assert us.authenticate("alice", "password-1").is_admin
    assert [u.username for u in us.list_users()] == ["alice", "bob"]
    assert us.delete_user("bob") and not us.delete_user("bob")
    with pytest.raises(ValueError):
        us.set_user("x", "root", "p")


# ---------------------------------------------------------------- database
def test_migrations_match_orm_and_enforce_constraints(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.migrate() == 4
    assert db.migrate() == 4  # idempotent
    insp = inspect(db.engine)
    for table in Base.metadata.sorted_tables:
        cols = {c["name"] for c in insp.get_columns(table.name)}
        assert {c.name for c in table.columns} == cols, table.name

    con = sqlite3.connect(tmp_path / "t.db")
    ins = ("INSERT INTO streams (name, source_rel_path, source_fingerprint, playback_mode, desired_state, "
           "status, created_at, updated_at, playback_speed, start_offset_s, end_offset_s) "
           "VALUES (?, 'a.mp4', '{}', 'loop', 'STOPPED', 'STOPPED', 'now', 'now', ?, ?, ?)")
    con.execute(ins, ("Cam1", 1.0, 0, None))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("cam1", 1.0, 0, None))  # case-insensitive unique name
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("cam2", 2.0, 0, None))  # speed fixed at 1x
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("cam3", 1.0, 10, 5))  # end before start
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("cam4", 1.0, -1, None))  # negative start
    con.close()


def test_migration_upgrades_a_v1_database_in_place(tmp_path):
    """A database created by v1 is upgraded without losing streams; owner backfilled."""
    import sqlite3 as sq
    from app.database.migrations import MIGRATIONS
    path = tmp_path / "old.db"
    con = sq.connect(path)
    con.executescript(f"BEGIN;{MIGRATIONS[0][1]}PRAGMA user_version = 1;COMMIT;")
    con.execute("INSERT INTO streams (name, source_rel_path, source_fingerprint, playback_mode, desired_state, "
                "status, created_at, updated_at, created_by) VALUES ('cam','a.mp4','{}','loop','RUNNING',"
                "'RUNNING','now','now','alice')")
    con.commit()
    con.close()
    assert Database(path).migrate() == 4
    con = sq.connect(path)
    assert con.execute("select name, owner, protected, group_id, slot_id from streams").fetchone() == \
        ("cam", "alice", 0, None, None)
    assert con.execute("select count(*) from analytics").fetchone() == (17,)


def test_login_throttle():
    from app.services.auth import LoginThrottle
    th = LoginThrottle(free=3, window=60, base_lock=10, max_lock=40)
    for _ in range(2):
        th.failure("ip:1", "user:a")
    assert th.retry_after("ip:1") == 0  # still free attempts
    th.failure("ip:1", "user:a")
    assert 9 < th.retry_after("ip:1") <= 10
    th.failure("ip:1", "user:a")
    assert 19 < th.retry_after("user:a") <= 20  # exponential
    for _ in range(5):
        th.failure("ip:1")
    assert th.retry_after("ip:1") <= 40  # capped
    th.success("user:a")
    assert th.retry_after("user:a") == 0 and th.retry_after("ip:1") > 0
