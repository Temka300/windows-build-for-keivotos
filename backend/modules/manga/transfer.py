"""manga selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.manga.user_schema import import_catalog_records
    from modules.manga.metadata import import_catalog_records as import_metadata_records,SOURCE,HEAD
    return TransferOwner('manga',catalog_importers=(CatalogImporter('manga.essential-records',1,import_catalog_records),CatalogImporter('manga.metadata-records',1,import_metadata_records)),content_namespaces=(SOURCE,HEAD),
        operational_table='',operational_index=False,select_records=select_records,
        personal_tables=('manga_categories', 'manga_reading_sessions', 'manga_reading_progress', 'manga_category_memberships', 'manga_category_versions', 'manga_category_updates', 'manga_metadata_overrides', 'manga_metadata_updates'))

OWNER='manga'
TABLES=('manga_categories', 'manga_reading_sessions', 'manga_reading_progress', 'manga_category_memberships', 'manga_category_versions', 'manga_category_updates', 'manga_metadata_overrides', 'manga_metadata_updates')


def select_records(selection):
    from services.collection_transfer import rows,records_for_values,admitted_path
    db=selection.context.connection;source_id=selection.context.source.source_id
    selected={}
    for table in TABLES:
        if table.endswith('_categories'):continue
        selected[table]=[r for r in rows(db,table,'source_id=?',(source_id,)) if admitted_path(selection.context,r['path'])]
    ids={r['category_id'] for r in selected.get(OWNER+'_category_memberships',[])}
    yield OWNER+'_categories',records_for_values(db,OWNER+'_categories','id',ids)
    for table in TABLES:
        if table in selected:yield table,selected[table]
