"""Experimental preserved posts; original archives and index facts belong to Files."""
from pathlib import Path

from module_descriptor import ModuleDescriptor
from modules.twitter.user_schema import SCHEMA, publish


def _routers():
    from modules.twitter.router import router
    return [router]


def _startup():
    from modules.twitter.jobs import recover
    recover()


def _tasks():
    from modules.twitter.jobs import run_queue
    return [('twitter-downloads', run_queue())]


def adopt(source_id: str) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        if sources.get_source(connection, source_id) is None:
            raise ValueError('Unknown folder')
        sources.update_source(connection, source_id, role='twitter')
    return {'source_id': source_id, 'role': 'twitter'}


def release(source_id: str, forget: bool = False) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        source = sources.get_source(connection, source_id)
        if source is None or source.role != 'twitter':
            raise ValueError('Unknown Twitter folder')
        if forget:
            sources.remove_source(connection, source_id)
        else:
            sources.update_source(connection, source_id, role='files')
    return {'source_id': source_id, 'role': 'files', 'forgotten': forget}


def descriptor(suite_home: Path, version: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        slug='twitter', name='Twitter', description='Locally preserved posts.',
        home=suite_home / 'modules/twitter', database=suite_home / 'base/files.sqlite',
        credentials=None, api_prefix='/api/twitter', log_prefix='twitter',
        user_agent=f'Keivotos/{version} (Twitter)', disableable=True, is_base=False,
        experimental=True, router_provider=_routers, adopt_hook=adopt, release_hook=release, shared_folder_relocation=True,
        user_schema_provider=lambda: SCHEMA, publish_hook=publish,
        startup_hook=_startup, background_tasks_hook=_tasks,
    )
