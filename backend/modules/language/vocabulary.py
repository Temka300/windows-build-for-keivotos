"""Personal vocabulary: exact authored text, append-only saves and retry receipts."""
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VocabularyContent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    korean: str = Field(min_length=1, max_length=256, strict=True)
    pronunciation: str = Field(default='', max_length=256, strict=True)
    english: str = Field(min_length=1, max_length=2000, strict=True)

    @field_validator('korean', 'english')
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Enter a word and an English translation')
        return value  # Do not trim, normalize, translate or rewrite authored text.


class VocabularyCreate(VocabularyContent):
    request_id: UUID


class VocabularyEntry(VocabularyContent):
    id: UUID
    created_at: str


class VocabularyPage(BaseModel):
    items: list[VocabularyEntry] = Field(max_length=100)
    total: int = Field(ge=0)


class VocabularyMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    revision: int = Field(ge=0, le=9007199254740990, strict=True)


class VocabularyEdit(VocabularyContent, VocabularyMutation):
    pass


class VocabularyVersion(VocabularyEntry):
    revision: int = Field(ge=0)
    archived: bool


class RequestConflict(ValueError):
    """The same save token cannot authorize different authored text."""


class EntryMissing(ValueError):
    pass


_COLUMNS = 'id,korean,pronunciation,english,created_at'
_CURRENT = '''v.id,v.created_at,coalesce(r.revision,0) AS revision,
    coalesce(r.kind='archive',0) AS archived,
    CASE WHEN r.revision IS NULL THEN v.korean ELSE r.korean END AS korean,
    CASE WHEN r.revision IS NULL THEN v.pronunciation ELSE r.pronunciation END AS pronunciation,
    CASE WHEN r.revision IS NULL THEN v.english ELSE r.english END AS english'''
_LATEST = '''FROM language_vocabulary v LEFT JOIN language_vocabulary_revisions r
    ON r.entry_id=v.id AND r.revision=(SELECT max(revision) FROM language_vocabulary_revisions WHERE entry_id=v.id)'''


def _entry(row) -> VocabularyEntry:
    return VocabularyEntry(**{key: row[key] for key in VocabularyEntry.model_fields})


def listing(connection, offset: int, limit: int) -> VocabularyPage:
    # Count and rows share a read snapshot, even if another process saves a word.
    with connection:
        connection.execute('BEGIN')
        active=" WHERE r.kind IS NULL OR r.kind!='archive'"
        total = connection.execute('SELECT COUNT(*) AS total '+_LATEST+active).fetchone()['total']
        rows = connection.execute('SELECT '+_CURRENT+' '+_LATEST+active+
                                  ' ORDER BY v.rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return VocabularyPage(items=[_entry(row) for row in rows], total=total)


def create(connection, payload: VocabularyCreate) -> VocabularyEntry:
    connection.execute('BEGIN IMMEDIATE')
    try:
        token = str(payload.request_id)
        if connection.execute('SELECT 1 FROM language_vocabulary_revisions WHERE request_id=?',(token,)).fetchone():
            raise RequestConflict('This request already changed a saved vocabulary entry.')
        row = connection.execute(f'SELECT {_COLUMNS} FROM language_vocabulary WHERE request_id=?',
                                 (token,)).fetchone()
        if row is not None:
            if (row['korean'], row['pronunciation'], row['english']) != (payload.korean, payload.pronunciation, payload.english):
                raise RequestConflict('This save already stored different vocabulary details. Reload Vocabulary before saving a changed entry.')
        else:
            identity = str(uuid4())
            connection.execute('INSERT INTO language_vocabulary '
                               '(id,request_id,korean,pronunciation,english) VALUES (?,?,?,?,?)',
                               (identity, token, payload.korean, payload.pronunciation, payload.english))
            row = connection.execute(f'SELECT {_COLUMNS} FROM language_vocabulary WHERE id=?',
                                     (identity,)).fetchone()
        result = _entry(row)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def read(connection, identity: UUID) -> VocabularyVersion:
    row=connection.execute('SELECT '+_CURRENT+' '+_LATEST+' WHERE v.id=?',(str(identity),)).fetchone()
    if row is None:raise EntryMissing('This vocabulary entry is unavailable.')
    return VocabularyVersion(**dict(row))


def _change(connection, identity: UUID, payload: VocabularyMutation, kind: str) -> VocabularyVersion:
    connection.execute('BEGIN IMMEDIATE')
    try:
        identity=str(identity);token=str(payload.request_id)
        receipt=connection.execute('SELECT * FROM language_vocabulary_revisions WHERE request_id=?',(token,)).fetchone()
        if receipt is not None:
            same=(receipt['entry_id']==identity and receipt['kind']==kind and receipt['revision']==payload.revision+1)
            if kind=='edit':same=same and all(receipt[key]==getattr(payload,key) for key in ('korean','pronunciation','english'))
            if not same:raise RequestConflict('This request already saved different vocabulary changes.')
            original=connection.execute('SELECT created_at FROM language_vocabulary WHERE id=?',(identity,)).fetchone()
            result=VocabularyVersion(id=identity,created_at=original['created_at'],revision=receipt['revision'],
                archived=kind=='archive',**{key:receipt[key] for key in ('korean','pronunciation','english')})
        else:
            if connection.execute('SELECT 1 FROM language_vocabulary WHERE request_id=?',(token,)).fetchone():
                raise RequestConflict('This request already created a vocabulary entry.')
            current=read(connection,identity)
            if current.archived or current.revision!=payload.revision:
                raise RequestConflict('This entry changed or was deleted. Reload the saved entry before changing it.')
            values={key:getattr(payload if kind=='edit' else current,key) for key in ('korean','pronunciation','english')}
            connection.execute('INSERT INTO language_vocabulary_revisions '
                '(entry_id,revision,request_id,kind,korean,pronunciation,english) VALUES(?,?,?,?,?,?,?)',
                (identity,current.revision+1,token,kind,values['korean'],values['pronunciation'],values['english']))
            result=VocabularyVersion(id=identity,created_at=current.created_at,revision=current.revision+1,
                archived=kind=='archive',**values)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def edit(connection,identity:UUID,payload:VocabularyEdit)->VocabularyVersion:
    return _change(connection,identity,payload,'edit')


def archive(connection,identity:UUID,payload:VocabularyMutation)->VocabularyVersion:
    return _change(connection,identity,payload,'archive')
