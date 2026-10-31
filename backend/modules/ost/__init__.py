"""Experimental local soundtrack folders; originals and facts belong to Files."""
from pathlib import Path

from module_descriptor import ModuleDescriptor


def _routers():
    from modules.ost.router import router
    return [router]


def _file_metadata(context):
    from modules.ost.library import file_metadata
    return file_metadata(context)


def adopt(source_id: str) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        if sources.get_source(connection, source_id) is None:
            raise ValueError("Unknown folder")
        sources.update_source(connection, source_id, role="ost")
    return {"source_id": source_id, "role": "ost"}


def release(source_id: str, forget: bool = False) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        source = sources.get_source(connection, source_id)
        if source is None or source.role != "ost":
            raise ValueError("Unknown Music folder")
        if forget:
            sources.remove_source(connection, source_id)
        else:
            sources.update_source(connection, source_id, role="files")
    return {"source_id": source_id, "role": "files", "forgotten": forget}


def descriptor(suite_home: Path, version: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        portable_provider=_portable,
        transfer_provider=_transfer,
        slug="ost", name="Music", description="Local music folders.",
        home=suite_home / "modules" / "ost", database=suite_home / "base" / "files.sqlite",
        credentials=None, api_prefix="/api/ost", log_prefix="ost",
        user_agent=f"Keivotos/{version} (OST)", disableable=True, is_base=False,
        experimental=True, router_provider=_routers, adopt_hook=adopt, release_hook=release, shared_folder_relocation=True,
        file_metadata_provider=_file_metadata,
        catalog_index_provider=_catalog_index,
        index_rescan_hook=_rescan_details,
        startup_hook=_metadata_startup,
        background_tasks_hook=_metadata_tasks,
    )


def _catalog_index():
    from modules.ost.albums import initialize_index, rebuild_index
    from services.catalog_index import IndexOwner
    return IndexOwner('ost', ('ost_catalog',), initialize_index, rebuild_index,
                      requires=('files',), legacy_tables=('files_index','files_audio_metadata'))


def _portable():
    from modules.ost.portable import contribution
    return contribution()


def _transfer():
    from modules.ost.transfer import contribution
    return contribution()


def _rescan_details(source_id):
    from modules.ost.metadata_jobs import request
    return {"metadata_job": request(source_id, "", discover=False)}


def _metadata_startup():
    from modules.ost.metadata_jobs import startup
    startup()


def _metadata_tasks():
    from modules.ost.metadata_jobs import worker
    return [("ost-metadata", worker())]
