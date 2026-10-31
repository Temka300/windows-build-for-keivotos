"""Explicit portable copies of Files selections; personal annotations stay local."""
from services.item_manifest import PortableOwner, local_description

def describe(context,selection,options):
    return local_description(context,selection,options,owner='files',item_type='file')

def contribution():
    return PortableOwner('files',describe)
