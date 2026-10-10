"""Personal playlists; additive tables in the shared user database."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_playlists (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 revision INTEGER NOT NULL DEFAULT 0, deleted_at TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS youtube_playlist_history (
 playlist_id TEXT NOT NULL, revision INTEGER NOT NULL, request_id TEXT NOT NULL,
 operation TEXT NOT NULL, previous_title TEXT NOT NULL, title TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(playlist_id,revision), UNIQUE(playlist_id,request_id)
);
"""


def migrate(connection):
    """Restartable additive migration; preserve every existing row."""
    columns={row[0] if not isinstance(row,dict) else row['name']
             for row in connection.execute("SELECT name FROM pragma_table_info('youtube_playlists')")}
    if 'revision' not in columns:
        connection.execute('ALTER TABLE youtube_playlists ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    if 'deleted_at' not in columns:
        connection.execute("ALTER TABLE youtube_playlists ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    connection.execute("""INSERT OR IGNORE INTO youtube_playlist_history
        (playlist_id,revision,request_id,operation,previous_title,title,created_at)
        SELECT id,0,request_id,'create','',title,created_at FROM youtube_playlists""")
