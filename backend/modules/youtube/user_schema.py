"""Personal playlists; additive tables in the shared user database."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_playlists (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS youtube_playlist_items (
 playlist_id TEXT NOT NULL, job_id TEXT NOT NULL, position INTEGER NOT NULL,
 added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(playlist_id,job_id), UNIQUE(playlist_id,position)
);
CREATE INDEX IF NOT EXISTS youtube_playlist_video ON youtube_playlist_items(job_id);
CREATE TABLE IF NOT EXISTS youtube_playlist_versions (
 job_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0
);
"""
