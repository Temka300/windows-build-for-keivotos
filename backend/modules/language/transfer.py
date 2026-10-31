"""language selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.language.user_schema import import_catalog_records
    return TransferOwner('language',catalog_importers=(CatalogImporter('language.essential-records',1,import_catalog_records),),
        operational_table='',operational_index=False)
