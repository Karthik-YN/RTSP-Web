"""Explicit, versioned SQLite schema migrations.

The schema is defined here in SQL (not generated from the ORM) so that an
existing database can be upgraded in place. ``PRAGMA user_version`` records
the applied version. To change the schema, append a new entry to MIGRATIONS;
never edit an entry that has shipped.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

# The analytics offered in the dashboard's left taskbar (v4 seed). People can
# add, rename and reorder them afterwards.
ANALYTICS_SEED: list[tuple[str, list[str]]] = [
    ("Traffic", ["Charging station", "videoANPR", "TrafficCongestion", "VehicleALPR", "VehicleDwell"]),
    ("People", ["Camera Tampering", "SlipAndFall", "CrowdDensity", "RegionCount", "LineCount", "PPE",
                "AbandonedObject", "DigitalFence", "Human Attributes"]),
    ("Earth Science", ["Elephant", "FireSmoke"]),
    ("Parking", ["Parking"]),
]
_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"


def _seed_sql() -> str:
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    lines = []
    for ci, (cat, names) in enumerate(ANALYTICS_SEED):
        lines.append(f"INSERT INTO analytics_categories (name, sort, created_at) VALUES ({q(cat)}, {ci}, {_NOW_SQL});")
        for ai, name in enumerate(names):
            lines.append(
                "INSERT INTO analytics (category_id, name, sort, created_at) "
                f"SELECT id, {q(name)}, {ai}, {_NOW_SQL} FROM analytics_categories WHERE name = {q(cat)};")
    return "\n".join(lines) + "\n"


_ANALYTICS_SEED_SQL = _seed_sql()

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE videos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path        TEXT    NOT NULL UNIQUE,
            size_bytes      INTEGER NOT NULL,
            mtime_ns        INTEGER NOT NULL,
            inode           INTEGER NOT NULL,
            device          INTEGER NOT NULL,
            quick_hash      TEXT    NOT NULL,
            status          TEXT    NOT NULL,          -- READY | INVALID | MISSING
            error           TEXT,
            container       TEXT,
            duration_s      REAL,
            width           INTEGER,
            height          INTEGER,
            fps             REAL,
            fps_text        TEXT,
            is_vfr          INTEGER NOT NULL DEFAULT 0,
            video_codec     TEXT,
            video_profile   TEXT,
            pix_fmt         TEXT,
            bit_depth       INTEGER,
            field_order     TEXT,
            bitrate_bps     INTEGER,
            audio_codec     TEXT,
            keyframe_interval_s REAL,
            compat_mode     TEXT,                      -- copy | transcode | reject
            compat_reasons  TEXT    NOT NULL DEFAULT '[]',
            compat_warnings TEXT    NOT NULL DEFAULT '[]',
            probe_json      TEXT,
            thumbnail       TEXT,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            last_seen_at    TEXT    NOT NULL
        );

        CREATE TABLE streams (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            video_id         INTEGER REFERENCES videos(id) ON DELETE SET NULL,
            source_rel_path  TEXT    NOT NULL,
            source_fingerprint TEXT  NOT NULL,         -- JSON: size, mtime_ns, inode, device, quick_hash
            start_offset_s   REAL    NOT NULL DEFAULT 0,
            effective_start_s REAL   NOT NULL DEFAULT 0,  -- snapped to keyframe
            end_offset_s     REAL,
            playback_mode    TEXT    NOT NULL,         -- loop | once
            playback_speed   REAL    NOT NULL DEFAULT 1.0 CHECK (playback_speed = 1.0),
            force_transcode  INTEGER NOT NULL DEFAULT 0,
            output_mode      TEXT,                     -- copy | transcode (decided at start)
            desired_state    TEXT    NOT NULL,         -- RUNNING | STOPPED
            status           TEXT    NOT NULL,         -- STOPPED | STARTING | RUNNING | STOPPING | RESTARTING | ERROR | COMPLETED
            status_detail    TEXT,
            pid              INTEGER,
            last_error       TEXT,
            loop_count       INTEGER NOT NULL DEFAULT 0,
            restart_count    INTEGER NOT NULL DEFAULT 0,
            ffmpeg_command   TEXT,                     -- JSON argv
            config_json      TEXT    NOT NULL DEFAULT '{}',
            version          INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT    NOT NULL,
            created_by       TEXT,
            updated_at       TEXT    NOT NULL,
            updated_by       TEXT,
            last_start_at    TEXT,
            last_stop_at     TEXT,
            running_since    TEXT,
            CHECK (playback_mode IN ('loop', 'once')),
            CHECK (desired_state IN ('RUNNING', 'STOPPED')),
            CHECK (start_offset_s >= 0),
            CHECK (end_offset_s IS NULL OR end_offset_s > start_offset_s)
        );
        CREATE INDEX ix_streams_video ON streams(video_id);
        CREATE INDEX ix_streams_desired ON streams(desired_state);

        CREATE TABLE events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            level       TEXT    NOT NULL,
            kind        TEXT    NOT NULL,
            stream_id   INTEGER,
            stream_name TEXT,
            username    TEXT,
            message     TEXT    NOT NULL,
            data_json   TEXT
        );
        CREATE INDEX ix_events_ts ON events(ts);
        CREATE INDEX ix_events_stream ON events(stream_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE stream_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT,
            created_by  TEXT,
            created_at  TEXT    NOT NULL
        );
        ALTER TABLE streams ADD COLUMN owner TEXT;
        ALTER TABLE streams ADD COLUMN note TEXT;
        ALTER TABLE streams ADD COLUMN protected INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE streams ADD COLUMN group_id INTEGER REFERENCES stream_groups(id) ON DELETE SET NULL;
        UPDATE streams SET owner = created_by;
        CREATE INDEX ix_streams_group ON streams(group_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE status_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id INTEGER NOT NULL,
            ts        TEXT    NOT NULL,
            status    TEXT    NOT NULL,
            detail    TEXT
        );
        CREATE INDEX ix_status_history_stream_ts ON status_history(stream_id, ts);
        """,
    ),
    (
        4,
        """
        CREATE TABLE analytics_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            sort        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL
        );
        CREATE TABLE analytics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES analytics_categories(id) ON DELETE RESTRICT,
            name        TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT,
            sort        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX ix_analytics_category ON analytics(category_id);
        CREATE TABLE analytics_slots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            analytics_id INTEGER NOT NULL REFERENCES analytics(id) ON DELETE RESTRICT,
            title        TEXT    NOT NULL,
            description  TEXT,
            video_id     INTEGER REFERENCES videos(id) ON DELETE SET NULL,
            sort         INTEGER NOT NULL DEFAULT 0,
            created_by   TEXT,
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL
        );
        CREATE INDEX ix_slots_analytics ON analytics_slots(analytics_id);
        ALTER TABLE streams ADD COLUMN slot_id INTEGER REFERENCES analytics_slots(id) ON DELETE SET NULL;
        CREATE INDEX ix_streams_slot ON streams(slot_id);
        """ + _ANALYTICS_SEED_SQL,
    ),
    (
        5,
        # Lead-in (slate before the video). Existing streams keep today's behaviour.
        """
        ALTER TABLE streams ADD COLUMN lead_in_mode TEXT NOT NULL DEFAULT 'none'
            CHECK (lead_in_mode IN ('none', 'fixed', 'hold'));
        ALTER TABLE streams ADD COLUMN lead_in_s REAL NOT NULL DEFAULT 5
            CHECK (lead_in_s >= 0 AND lead_in_s <= 120);
        ALTER TABLE streams ADD COLUMN hold_max_s REAL CHECK (hold_max_s IS NULL OR hold_max_s > 0);
        """,
    ),
    (
        6,
        # Settings changed from the dashboard (capacity limits after a load test).
        """
        CREATE TABLE app_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        log.info("Applying schema migration %s", version)
        # executescript() commits any pending transaction first; we wrap the
        # migration in an explicit transaction so it is all-or-nothing.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        current = version
    return current
