from __future__ import annotations

from fastapi import APIRouter, HTTPException
from local_recovery import local_recovery_status

router = APIRouter()


@router.get("/api/local-recovery")
def get_local_recovery():
    return local_recovery_status()


@router.post("/api/local-recovery/checkpoint")
def create_recovery_checkpoint():
    raise HTTPException(410, "Standalone checkpoints have been retired. Use Settings > Backup.")


@router.get("/api/local-recovery/collections")
def collection_sources():
    from database import get_user_db
    from files_base.sources import list_sources
    with get_user_db() as db:
        return [vars(source) for source in list_sources(db) if source.visible]


def _collection_error(operation):
    from files_base.serving import ServeDenied
    import sqlite3
    try:return operation()
    except (ValueError,OSError,ServeDenied,sqlite3.Error) as exc:
        raise HTTPException(400,str(exc)) from exc
    except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc


from models import CollectionExportRequest,CollectionImportRequest


def _export(request,preview):
    from contextlib import ExitStack
    from pathlib import Path
    import config
    from database import get_user_db
    from files_base import sources
    from services import item_manifest,collection_transfer
    if not request.source_ids or len(set(request.source_ids))!=len(request.source_ids):
        raise ValueError('Select distinct registered folders')
    with ExitStack() as stack:
        db=stack.enter_context(get_user_db())
        registered=sources.list_sources(db);selected=[]
        for source_id in request.source_ids:
            source=next((s for s in registered if s.source_id==source_id),None)
            if source is None or not source.visible:raise ValueError('Selected folder is unavailable')
            selected.append(item_manifest.Context(source,tuple(registered),config.SUITE_HOME,
                config.INDEX_DB_PATH,db,config.MODULE_REGISTRY))
        if preview:return collection_transfer.preview_export(selected,request.include_personal)
        from maintenance import exclusive_tool_operation
        with exclusive_tool_operation('exporting a collection',wait=False):
            return collection_transfer.export_collection(selected,Path(request.destination),
                request_id=request.request_id,include_personal=request.include_personal,configuration=config)


@router.post("/api/local-recovery/collections/export-preview")
def preview_collection_export(request:CollectionExportRequest):
    return _collection_error(lambda:_export(request,True))


@router.post("/api/local-recovery/collections/export")
def export_collection(request:CollectionExportRequest):
    return _collection_error(lambda:_export(request,False))


@router.post("/api/local-recovery/collections/import-preview")
def preview_collection_import(request:CollectionImportRequest):
    import config
    from services.collection_transfer import preview_import
    return _collection_error(lambda:preview_import(request.directory,request.destination,config,
                                                  include_personal=request.include_personal))


@router.post("/api/local-recovery/collections/import")
def import_collection(request:CollectionImportRequest):
    import config
    from services.collection_transfer import import_collection as perform
    return _collection_error(lambda:perform(request.directory,request.destination,config,
        request_id=request.request_id,include_personal=request.include_personal))


@router.post("/api/local-recovery/rebuild-index")
def rebuild_library_index():
    import config
    from services.storage_authority import rebuild
    return _collection_error(lambda:rebuild(config))
