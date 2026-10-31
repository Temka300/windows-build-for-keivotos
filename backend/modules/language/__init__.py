"""Experimental local Markdown notes attached to registered Language folders."""
from pathlib import Path

from module_descriptor import BackupComponent, ModuleDescriptor
from modules.language.user_schema import SCHEMA, publish


def _routers():
    from modules.language.router import router
    return [router]


def adopt(source_id: str) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        if sources.get_source(connection, source_id) is None:
            raise ValueError("Unknown folder")
        sources.update_source(connection, source_id, role="language")
    return {"source_id": source_id, "role": "language"}


def release(source_id: str, forget: bool = False) -> dict:
    from database import get_user_db
    from files_base import sources
    with get_user_db() as connection:
        source = sources.get_source(connection, source_id)
        if source is None or source.role != "language":
            raise ValueError("Unknown Language folder")
        if forget:
            sources.remove_source(connection, source_id)
        else:
            sources.update_source(connection, source_id, role="files")
    return {"source_id": source_id, "role": "files", "forgotten": forget}


def descriptor(suite_home: Path, version: str) -> ModuleDescriptor:
    home = suite_home / "modules" / "language"
    return ModuleDescriptor(
        portable_provider=_portable,
        transfer_provider=_transfer,
        slug="language", name="Language", description="Write local Markdown notes in tabs.",
        home=home, database=suite_home / "base" / "files.sqlite",
        credentials=None, api_prefix="/api/language", log_prefix="language",
        user_agent=f"Keivotos/{version} (Language)", disableable=True,
        is_base=False, experimental=True, router_provider=_routers,
        adopt_hook=adopt, release_hook=release, shared_folder_relocation=True,
        user_schema_provider=lambda: SCHEMA, publish_hook=publish,
        backup_components_provider=lambda: (
            BackupComponent("language_revisions", "language", "language_revisions",
                            "tree", suite_home / "assets" / "language" / "revisions"),
        ),
    )


def _portable():
    from modules.language.portable import contribution
    return contribution()


def _transfer():
    from modules.language.transfer import contribution
    return contribution()
