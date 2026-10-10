"""Additive personal study choices and vocabulary in the suite user database."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS language_study_preferences (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 category TEXT NOT NULL CHECK(category IN ('sino','native','months','dates','hours','minutes')),
 answer_language TEXT NOT NULL CHECK(answer_language IN ('korean','english')),
 minimum INTEGER NOT NULL CHECK(minimum >= 1),
 maximum INTEGER NOT NULL CHECK(maximum >= minimum AND maximum <= 99)
);
CREATE TABLE IF NOT EXISTS language_vocabulary (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 korean TEXT NOT NULL, pronunciation TEXT NOT NULL DEFAULT '', english TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS language_study_preferences_v2 (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 category TEXT NOT NULL CHECK(category IN ('sino','native','months','dates','hours','minutes','month_date','hour_minute','date_time')),
 answer_language TEXT NOT NULL CHECK(answer_language IN ('korean','english')),
 minimum INTEGER NOT NULL CHECK(minimum >= 1),
 maximum INTEGER NOT NULL CHECK(maximum >= minimum AND maximum <= 262800)
);
"""


def publish(connection):
    connection.executescript(SCHEMA)
    connection.commit()
