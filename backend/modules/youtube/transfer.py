"""youtube selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.youtube.user_schema import import_catalog_library
    return TransferOwner('youtube',content_namespaces=('youtube.download','youtube.library','youtube.asset-observation'),catalog_importers=(CatalogImporter('youtube.saved-library',1,import_catalog_library),),
        operational_table='youtube_downloads',operational_index=False,select_records=select_records,
        personal_tables=('youtube_playlists', 'youtube_playlist_items', 'youtube_playlist_versions', 'youtube_playlist_history'))


def select_records(selection):
    from services.collection_transfer import records_for_values
    db=selection.context.connection
    members=records_for_values(db,'youtube_playlist_items','job_id',selection.job_ids)
    playlists={row['playlist_id'] for row in members}
    yield 'youtube_playlists',records_for_values(db,'youtube_playlists','id',playlists)
    yield 'youtube_playlist_items',members
    yield 'youtube_playlist_versions',records_for_values(db,'youtube_playlist_versions','job_id',selection.job_ids)
    yield 'youtube_playlist_history',records_for_values(db,'youtube_playlist_history','playlist_id',playlists)
