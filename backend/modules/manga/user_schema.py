"""Additive personal reading state; original manga and caches are untouched."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS manga_reading_sessions (
 source_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(source_id, path, kind)
);
CREATE TABLE IF NOT EXISTS manga_reading_progress (
 source_id TEXT NOT NULL, series_path TEXT NOT NULL, path TEXT NOT NULL,
 kind TEXT NOT NULL, revision TEXT NOT NULL, page_count INTEGER NOT NULL,
 page INTEGER NOT NULL, mode TEXT NOT NULL, anchor REAL NOT NULL DEFAULT 0.5,
 generation INTEGER NOT NULL, sequence INTEGER NOT NULL,
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(source_id, path, kind, revision)
);
CREATE INDEX IF NOT EXISTS manga_progress_series
 ON manga_reading_progress(source_id, series_path, updated_at DESC);
"""


def publish(connection):
    connection.executescript(SCHEMA)
    connection.commit()
