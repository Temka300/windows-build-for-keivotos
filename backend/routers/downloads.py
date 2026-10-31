"""Passive suite Downloads API; optional owner runtimes are never required."""
from typing import Literal
import sqlite3
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from uuid import UUID
import psutil
from maintenance import module_operation
from files_base.serving import ServeDenied
from pydantic import BaseModel, Field
import config
from services import download_history, download_controls

router=APIRouter()


class DownloadHistoryItem(BaseModel):
    id: str
    owner: str
    module_name: str
    name: str
    state: Literal['queued','working','completed','failed','cancelled','interrupted']
    progress: float | None = Field(ge=0,le=100)
    started_at: str | None
    completed_at: str | None
    date_at: str | None
    date_source: Literal['request','transfer','recorded-capture','recorded-manifest','unknown']
    attempt: int = Field(ge=0)
    can_delete: bool = False
    control_version: str | None = None
    thumbnail_url: str | None = None


class DownloadHistoryPage(BaseModel):
    items: list[DownloadHistoryItem]
    next_cursor: str | None


@router.get('/api/downloads',response_model=DownloadHistoryPage)
def history(limit: int=Query(100,ge=1,le=200),after: str | None=Query(None,max_length=1024),
            owner: str | None=Query(None,max_length=128)):
    try:
        return download_history.page(jobs_path=config.JOBS_DB_PATH,user_path=config.USER_DB_PATH,
            registry=config.MODULE_REGISTRY,limit=limit,after=after,owner=owner)
    except (sqlite3.Error,RuntimeError) as exc:
        raise HTTPException(503,'Download history is unavailable; saved files are preserved.') from exc
    except ValueError as exc:
        if str(exc).startswith(('Invalid download history cursor','Invalid owner','Invalid history')):
            raise HTTPException(400,str(exc)) from exc
        raise HTTPException(503,'Download history is unavailable; saved files are preserved.') from exc


class ControlRequest(BaseModel):
    request_id: UUID


class DeleteRequest(ControlRequest):
    expected_version: str = Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]+$')
    confirm: bool = False


class DownloadControlState(BaseModel):
    paused: bool
    revision: int


class DownloadDeleteResult(BaseModel):
    deleted: bool
    requires_confirmation: bool
    file_count: int


def _control(action):
    try:
        with module_operation():
            return action()
    except ServeDenied as exc:
        raise HTTPException(exc.status_code,'Download file is unavailable.') from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    except (RuntimeError,sqlite3.Error,OSError,psutil.Error) as exc:
        raise HTTPException(503,'The download action could not finish. Files were preserved.') from exc


@router.get('/api/downloads/control',response_model=DownloadControlState)
def control_state():
    return _control(lambda: download_controls.state(config.JOBS_DB_PATH))


@router.post('/api/downloads/pause',response_model=DownloadControlState)
def pause_downloads(request: ControlRequest):
    return _control(lambda: download_controls.set_paused(jobs_path=config.JOBS_DB_PATH,
        paused_value=True,request_id=request.request_id))


@router.post('/api/downloads/resume',response_model=DownloadControlState)
def resume_downloads(request: ControlRequest):
    return _control(lambda: download_controls.set_paused(jobs_path=config.JOBS_DB_PATH,
        paused_value=False,request_id=request.request_id))


@router.get('/api/downloads/{item_id}',response_model=DownloadHistoryItem)
def history_item(item_id: str):
    result=_control(lambda: download_history.page(jobs_path=config.JOBS_DB_PATH,user_path=config.USER_DB_PATH,
        registry=config.MODULE_REGISTRY,limit=1,item_id=item_id))
    if not result['items']:raise HTTPException(404,'Download entry is unavailable.')
    return result['items'][0]


@router.get('/api/downloads/{item_id}/thumbnail')
def thumbnail(item_id: str):
    path=_control(lambda: download_controls.thumbnail(item_id=item_id,jobs_path=config.JOBS_DB_PATH,
        user_path=config.USER_DB_PATH,home=config.SUITE_HOME,registry=config.MODULE_REGISTRY))
    if path is None:raise HTTPException(404,'Thumbnail is unavailable.')
    return FileResponse(path,headers={'Cache-Control':'no-store'})


@router.post('/api/downloads/{item_id}/delete',response_model=DownloadDeleteResult)
def delete_download(item_id: str,request: DeleteRequest):
    return _control(lambda: download_controls.delete_active(item_id=item_id,expected_version=request.expected_version,
        request_id=request.request_id,confirm=request.confirm,jobs_path=config.JOBS_DB_PATH,
        user_path=config.USER_DB_PATH,home=config.SUITE_HOME,registry=config.MODULE_REGISTRY))
