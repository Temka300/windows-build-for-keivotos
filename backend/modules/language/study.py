"""Practice preferences only; exercises and typed answers are never stored here."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIMITS = {'sino': 99, 'native': 20, 'months': 12, 'dates': 31, 'hours': 12, 'minutes': 59}


class StudyPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['sino', 'native', 'months', 'dates', 'hours', 'minutes'] = 'sino'
    answer_language: Literal['korean', 'english'] = 'english'
    minimum: int = Field(default=1, ge=1, le=99, strict=True)
    maximum: int = Field(default=99, ge=1, le=99, strict=True)

    @model_validator(mode='after')
    def bounded_range(self):
        if not self.minimum <= self.maximum <= LIMITS[self.category]:
            raise ValueError('Choose a range within this category')
        return self


def read(connection) -> StudyPreferences:
    row = connection.execute('SELECT category,answer_language,minimum,maximum '
                             'FROM language_study_preferences WHERE id=1').fetchone()
    if row is None:
        return StudyPreferences()
    return StudyPreferences(**{key: row[key] for key in StudyPreferences.model_fields})


def save(connection, preferences: StudyPreferences) -> StudyPreferences:
    connection.execute('''INSERT INTO language_study_preferences
        (id,category,answer_language,minimum,maximum) VALUES (1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET category=excluded.category,
        answer_language=excluded.answer_language,minimum=excluded.minimum,maximum=excluded.maximum''',
        (preferences.category, preferences.answer_language, preferences.minimum, preferences.maximum))
    connection.commit()
    return preferences
