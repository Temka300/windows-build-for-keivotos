"""files selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from files_base.annotations import import_catalog_records
    return TransferOwner('files',catalog_importers=(CatalogImporter('files.annotations',1,import_catalog_records),),
        operational_table='',operational_index=False,reference_files=references,select_records=select_records,
        personal_tables=("files_annotations","files_annotation_links","files_annotation_attachments"))


def references(db,configuration):
    from pathlib import Path
    from files_base import attachment_store
    from services.asset_preservation import Reference,_table
    if not _table(db,'files_annotation_attachments'):return
    cursor=db.cursor()
    import sqlite3
    cursor.row_factory=sqlite3.Row
    try:
        for row in cursor.execute('SELECT id,content_hash,file_name,media_type,size,stored_root FROM files_annotation_attachments'):
            if not row['stored_root']:continue
            ext=attachment_store.extension_for(row['file_name'],row['media_type'])
            yield Reference('files','attachment:'+str(row['id']),
                attachment_store.resolve_attachment(row['stored_root'],row['content_hash'],ext),
                'md5',row['content_hash'],row['size'])
    finally:cursor.close()


def select_records(selection):
    from services.collection_transfer import rows,records_for_values,admitted_path
    db=selection.context.connection;source_id=selection.context.source.source_id
    notes=[row for row in rows(db,'files_annotations')
           if (row['source_id']==source_id and admitted_path(selection.context,row['relative_path'])) or row['content_hash'] in selection.hashes]
    ids={row['id'] for row in notes}
    yield 'files_annotations',notes
    for table in ('files_annotation_links','files_annotation_attachments'):
        yield table,records_for_values(db,table,'annotation_id',ids)
