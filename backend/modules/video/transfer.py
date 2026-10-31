"""video selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.video.user_schema import import_catalog_records
    from modules.video.progress import import_catalog_records as import_watch_records
    from modules.video.metadata import import_catalog_records as import_metadata_records,SOURCE,HEAD
    return TransferOwner('video',catalog_importers=(CatalogImporter('video.essential-records',1,import_catalog_records),CatalogImporter('video.watch-records',1,import_watch_records),CatalogImporter('video.metadata-records',1,import_metadata_records)),
        operational_table='',operational_index=False,select_records=select_records,content_namespaces=(SOURCE,HEAD),
        personal_tables=('video_categories', 'video_category_memberships', 'video_category_versions', 'video_category_updates', 'video_watch_sessions', 'video_watch_progress', 'video_metadata_overrides', 'video_metadata_updates'))

OWNER='video'
TABLES=('video_categories', 'video_category_memberships', 'video_category_versions', 'video_category_updates', 'video_watch_sessions', 'video_watch_progress', 'video_metadata_overrides', 'video_metadata_updates')


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
