"""Additive, owner-scoped library categories and durable create receipts."""
import sqlite3
import json
import unicodedata
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=1024)
    request_id: UUID


class Category(BaseModel):
    id: str
    name: str
    created_at: str


class CategoryList(BaseModel):
    items: list[Category]
    total: int


class CategoryItem(BaseModel):
    source_id: str = Field(min_length=1, max_length=256)
    path: str = Field(max_length=4096)


class CategoryMembership(BaseModel):
    version: int
    category_ids: list[str]


class CategoryMembershipSave(CategoryItem):
    category_ids: list[UUID] = Field(max_length=256)
    version: int = Field(ge=0, le=9007199254740990)
    request_id: UUID


class CategoryStore:
    def __init__(self, owner: str):
        # Table names come only from installed owner code, never request data.
        if owner not in ('video', 'manga'):
            raise ValueError('Unsupported category owner')
        self.table = owner + '_categories'
        self.memberships = owner + '_category_memberships'
        self.versions = owner + '_category_versions'
        self.updates = owner + '_category_updates'
        self.schema = f"""
CREATE TABLE IF NOT EXISTS {self.table} (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS {self.memberships} (
 category_id TEXT NOT NULL REFERENCES {self.table}(id),
 source_id TEXT NOT NULL, path TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(category_id, source_id, path)
);
CREATE INDEX IF NOT EXISTS {self.memberships}_item
 ON {self.memberships}(source_id, path);
CREATE TABLE IF NOT EXISTS {self.versions} (
 source_id TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY(source_id, path)
);
CREATE TABLE IF NOT EXISTS {self.updates} (
 request_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, path TEXT NOT NULL,
 expected_version INTEGER NOT NULL, version INTEGER NOT NULL,
 category_ids TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

    def migrate(self, connection):
        columns = {row[1] if isinstance(row, tuple) else row['name']
                   for row in connection.execute(f'PRAGMA table_info({self.memberships})')}
        if 'active' not in columns:
            connection.execute(f'ALTER TABLE {self.memberships} ADD COLUMN active INTEGER NOT NULL DEFAULT 1')

    def publish(self, connection):
        connection.executescript(self.schema)
        self.migrate(connection)
        connection.commit()

    def require_category(self, connection, identity):
        if identity != 'default' and not connection.execute(
                f'SELECT 1 FROM {self.table} WHERE id=?', (identity,)).fetchone():
            raise HTTPException(404, 'Category is unavailable. Reload the category list.')

    def filter_sql(self, identity, alias='f', database='category_user'):
        category = '' if identity == 'default' else ' AND m.category_id=?'
        predicate = (f'EXISTS (SELECT 1 FROM {database}.{self.memberships} m '
                     f'WHERE m.source_id={alias}.source_id AND m.path={alias}.relative_path AND m.active=1{category})')
        return ('NOT ' + predicate if identity == 'default' else predicate), ([] if identity == 'default' else [identity])

    def filter_items(self, connection, items, identity):
        if identity is None:
            return items
        self.require_category(connection, identity)
        where = '' if identity == 'default' else ' AND category_id=?'
        params = [] if identity == 'default' else [identity]
        keys = {(row['source_id'], row['path']) for row in connection.execute(
            f'SELECT source_id,path FROM {self.memberships} WHERE active=1{where}', params)}
        return [item for item in items if ((item['source_id'], item['path']) in keys) == (identity != 'default')]

    def membership(self, connection, source_id, path):
        version = connection.execute(f'SELECT version FROM {self.versions} WHERE source_id=? AND path=?',
                                     (source_id, path)).fetchone()
        return {'version': version['version'] if version else 0,
                'category_ids': [row['category_id'] for row in connection.execute(
                    f'SELECT category_id FROM {self.memberships} WHERE source_id=? AND path=? AND active=1 ORDER BY category_id',
                    (source_id, path))]}

    def set_membership(self, connection, source_id, path, selected, expected, request_id, validate):
        selected = sorted(set(selected))
        try:
            connection.execute('BEGIN IMMEDIATE')
            validate(connection, source_id, path)
            for identity in selected:
                if identity == 'default':
                    raise HTTPException(400, 'Default is implicit. Clear the selected categories instead.')
                self.require_category(connection, identity)
            current = self.membership(connection, source_id, path)
            receipt = connection.execute(f'SELECT * FROM {self.updates} WHERE request_id=?', (request_id,)).fetchone()
            if receipt:
                if (receipt['source_id'] != source_id or receipt['path'] != path or receipt['expected_version'] != expected
                        or json.loads(receipt['category_ids']) != selected or receipt['version'] != current['version']):
                    raise HTTPException(409, 'Category membership changed. Reopen the picker before saving.')
                return current
            if current['version'] != expected:
                raise HTTPException(409, 'Category membership changed. Reopen the picker before saving.')
            version = expected + (current['category_ids'] != selected)
            for identity in set(current['category_ids']) - set(selected):
                connection.execute(f'UPDATE {self.memberships} SET active=0 WHERE source_id=? AND path=? AND category_id=?',
                                   (source_id, path, identity))
            for identity in set(selected) - set(current['category_ids']):
                connection.execute(f'INSERT INTO {self.memberships}(category_id,source_id,path,active) VALUES(?,?,?,1) '
                                   'ON CONFLICT(category_id,source_id,path) DO UPDATE SET active=1', (identity, source_id, path))
            connection.execute(f'INSERT INTO {self.versions} VALUES(?,?,?) ON CONFLICT(source_id,path) DO UPDATE SET version=excluded.version',
                               (source_id, path, version))
            connection.execute(f'INSERT INTO {self.updates}(request_id,source_id,path,expected_version,version,category_ids) VALUES(?,?,?,?,?,?)',
                               (request_id, source_id, path, expected, version, json.dumps(selected)))
            connection.commit()
            return {'version': version, 'category_ids': selected}
        except sqlite3.Error as exc:
            raise HTTPException(503, 'Could not save category membership. Try again.') from exc
        finally:
            if connection.in_transaction:
                connection.rollback()

    def listing(self, connection, offset: int, limit: int):
        total = connection.execute(f'SELECT count(*) AS total FROM {self.table}').fetchone()['total']
        rows = connection.execute(
            f'SELECT id,name,created_at FROM {self.table} ORDER BY rowid LIMIT ? OFFSET ?',
            (limit, offset)).fetchall()
        return {'items': [dict(row) for row in rows], 'total': total}

    def create(self, connection, name: str, request_id: str):
        name = unicodedata.normalize('NFC', name.strip())
        key = name.casefold()
        if not name or len(name) > 100:
            raise HTTPException(400, 'Enter a category name of 1–100 characters.')
        if key == 'default':
            raise HTTPException(400, 'Default is reserved for items without a category.')
        try:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute(
                f'SELECT id,name,created_at FROM {self.table} WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['name'] != name:
                    raise HTTPException(409, 'This request already created a different category.')
                return dict(previous)
            if connection.execute(f'SELECT 1 FROM {self.table} WHERE name_key=?', (key,)).fetchone():
                raise HTTPException(409, 'A category with this name already exists.')
            identity = str(uuid4())
            connection.execute(f'INSERT INTO {self.table}(id,request_id,name,name_key) VALUES(?,?,?,?)',
                               (identity, request_id, name, key))
            result = dict(connection.execute(
                f'SELECT id,name,created_at FROM {self.table} WHERE id=?', (identity,)).fetchone())
            connection.commit()
            return result
        except sqlite3.Error as exc:
            raise HTTPException(503, 'Could not save the category. Try again.') from exc
        finally:
            if connection.in_transaction:
                connection.rollback()
