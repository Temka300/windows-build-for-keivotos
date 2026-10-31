"""Guarded HTTP operations for local Language Markdown files."""
from __future__ import annotations
import logging
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

import config
import lifecycle
from database import get_user_db
from files_base import sources
from modules.language import documents, index_refresh, study, vocabulary
from suite_modules import require_enabled


def _enabled() -> None:
    try:
        require_enabled("language")
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


router = APIRouter(prefix="/api/language", dependencies=[Depends(_enabled)])
logger = logging.getLogger('keivotos.language')


def _study_call(operation, *args):
    try:
        with get_user_db() as connection:
            return operation(connection, *args)
    except (sqlite3.Error, OSError, ValueError) as error:
        logger.warning('Language practice settings unavailable: %s', error)
        raise HTTPException(503, 'Practice settings could not be loaded or saved. Retry when user data is available.') from error


@router.get('/study/preferences', response_model=study.StudyPreferences)
def study_preferences(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return _study_call(study.read)


@router.put('/study/preferences', response_model=study.StudyPreferences)
def save_study_preferences(payload: study.StudyPreferences):
    with lifecycle.module_change_lock:
        _enabled()
        return _study_call(study.save, payload)


def _vocabulary_call(operation, *args):
    try:
        with get_user_db() as connection:
            return operation(connection, *args)
    except vocabulary.EntryMissing as error:
        raise HTTPException(404, str(error)) from error
    except vocabulary.RequestConflict as error:
        raise HTTPException(409, str(error)) from error
    except (sqlite3.Error, OSError, ValueError) as error:
        # Invalid stored rows are preserved; do not log their authored contents.
        detail = str(error) if isinstance(error, (sqlite3.Error, OSError)) else 'Invalid stored vocabulary record'
        logger.warning('Language vocabulary unavailable: %s', detail)
        raise HTTPException(503, 'Vocabulary is unavailable. Keep your entry and retry when user data is available.') from error


@router.get('/vocabulary', response_model=vocabulary.VocabularyPage)
def list_vocabulary(response: Response, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    response.headers['Cache-Control'] = 'no-store'
    return _vocabulary_call(vocabulary.listing, offset, limit)


@router.post('/vocabulary', response_model=vocabulary.VocabularyEntry)
def create_vocabulary(payload: vocabulary.VocabularyCreate):
    with lifecycle.module_change_lock:
        _enabled()
        return _vocabulary_call(vocabulary.create, payload)


@router.get('/vocabulary/{entry_id}', response_model=vocabulary.VocabularyVersion)
def read_vocabulary(entry_id: UUID, response: Response):
    response.headers['Cache-Control']='no-store'
    return _vocabulary_call(vocabulary.read, entry_id)


@router.put('/vocabulary/{entry_id}', response_model=vocabulary.VocabularyVersion)
def edit_vocabulary(entry_id: UUID, payload: vocabulary.VocabularyEdit):
    with lifecycle.module_change_lock:
        _enabled()
        return _vocabulary_call(vocabulary.edit, entry_id, payload)


@router.post('/vocabulary/{entry_id}/archive', response_model=vocabulary.VocabularyVersion)
def archive_vocabulary(entry_id: UUID, payload: vocabulary.VocabularyMutation):
    with lifecycle.module_change_lock:
        _enabled()
        return _vocabulary_call(vocabulary.archive, entry_id, payload)


def _source(connection, source_id: str) -> sources.Source:
    source = sources.get_source(connection, source_id)
    if source is None or source.role != "language" or not source.visible:
        raise HTTPException(404, "Language folder is unavailable")
    return source


def _document_call(operation, *args):
    try:
        return operation(*args)
    except documents.DocumentError as error:
        raise HTTPException(error.status_code, error.detail) from error


class CreateRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=256)


class IndexRefreshRequest(CreateRequest):
    name: str = Field(min_length=1, max_length=256)
    previous_names: list[str] = Field(default_factory=list, max_length=64)


class RenameRequest(CreateRequest):
    name: str = Field(min_length=1, max_length=256)
    new_name: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=64, max_length=64)


class SaveRequest(CreateRequest):
    name: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=documents.MAX_BYTES)
    revision: str = Field(min_length=64, max_length=64)
    session_id: str = Field(min_length=32, max_length=32)


@router.get("/folders")
def folders():
    with get_user_db() as connection:
        return [{"source_id": source.source_id, "name": source.display_name}
                for source in sources.list_sources(connection) if source.role == "language" and source.visible]


@router.get("/documents")
def list_documents(source_id: str = Query(..., max_length=256)):
    with get_user_db() as connection:
        source = _source(connection, source_id)
        return {"files": _document_call(documents.list_documents, source, config.SUITE_HOME)}


@router.get("/document")
def read_document(source_id: str = Query(..., max_length=256), name: str = Query(..., max_length=256)):
    with get_user_db() as connection:
        source = _source(connection, source_id)
        return _document_call(documents.read_document, source, config.SUITE_HOME, name)


@router.post("/documents")
def create_document(payload: CreateRequest):
    with lifecycle.module_change_lock:
        _enabled()
        with get_user_db() as connection:
            source = _source(connection, payload.source_id)
            result = _document_call(documents.create_document, source, config.SUITE_HOME)
            result["index_refresh"] = index_refresh.attempt(source, config.FILES_DB_PATH, result["name"])
            return result


@router.post("/document/rename")
def rename_document(payload: RenameRequest):
    with lifecycle.module_change_lock:
        _enabled()
        with get_user_db() as connection:
            source = _source(connection, payload.source_id)
            result = _document_call(documents.rename_document, source, config.SUITE_HOME,
                                    payload.name, payload.new_name, payload.revision)
            result["index_refresh"] = index_refresh.attempt(
                source, config.FILES_DB_PATH, result["name"], [payload.name])
            return result


@router.put("/document")
def save_document(payload: SaveRequest):
    with lifecycle.module_change_lock:
        _enabled()
        with get_user_db() as connection:
            source = _source(connection, payload.source_id)
            result = _document_call(documents.save_document, source, config.SUITE_HOME,
                                    config.ASSETS_DIR / "language",
                                    payload.name, payload.content, payload.revision,
                                    payload.session_id)
            result["index_refresh"] = index_refresh.attempt(source, config.FILES_DB_PATH, result["name"])
            return result


@router.post("/document/index")
def refresh_document_index(payload: IndexRefreshRequest):
    with lifecycle.module_change_lock:
        _enabled()
        with get_user_db() as connection:
            source = _source(connection, payload.source_id)
            # Read and validate the current original, without replacing editor text.
            _document_call(documents.read_document, source, config.SUITE_HOME, payload.name)
            for name in payload.previous_names:
                _document_call(documents.valid_name, name)
            return index_refresh.attempt(source, config.FILES_DB_PATH, payload.name, payload.previous_names)
