"""Additive personal preferences and durable capture receipts; originals stay in Files."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS twitter_preferences (
 id INTEGER PRIMARY KEY CHECK(id=1), source_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS twitter_downloads (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
 post_id TEXT NOT NULL, source_id TEXT NOT NULL, root TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'accepted',
 progress REAL NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
 capture_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', retry_requests TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS twitter_downloads_queue ON twitter_downloads(status,created_at,id);
'''


def publish(connection):
    connection.executescript(SCHEMA)
    connection.commit()
