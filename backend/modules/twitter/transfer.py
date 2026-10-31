"""twitter selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.twitter.user_schema import import_catalog_receipts
    return TransferOwner('twitter',content_namespaces=('twitter.download','twitter.asset-observation'),catalog_importers=(CatalogImporter('twitter.saved-receipts',1,import_catalog_receipts),),
        operational_table='twitter_downloads',operational_index=False)
