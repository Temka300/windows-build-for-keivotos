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


class RequestConflict(ValueError):
    """The same save token cannot authorize different authored text."""


_COLUMNS = 'id,korean,pronunciation,english,created_at'


def _entry(row) -> VocabularyEntry:
    return VocabularyEntry(**{key: row[key] for key in VocabularyEntry.model_fields})


def listing(connection, offset: int, limit: int) -> VocabularyPage:
    # Count and rows share a read snapshot, even if another process saves a word.
    with connection:
        connection.execute('BEGIN')
        total = connection.execute('SELECT COUNT(*) AS total FROM language_vocabulary').fetchone()['total']
        rows = connection.execute(f'SELECT {_COLUMNS} FROM language_vocabulary '
                                  'ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return VocabularyPage(items=[_entry(row) for row in rows], total=total)


def create(connection, payload: VocabularyCreate) -> VocabularyEntry:
    connection.execute('BEGIN IMMEDIATE')
    try:
        token = str(payload.request_id)
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
