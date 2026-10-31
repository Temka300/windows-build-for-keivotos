"""Markdown stays the authored file; export never synthesizes study records."""
from services.item_manifest import PortableOwner, local_description
from modules.language import documents

def describe(context,selection,options):
    context.resolve(selection)
    note=documents.read_document(context.source,context.suite_home,selection)
    return local_description(context,selection,options,owner='language',item_type='document',
                             facts={'name':note['name'],'revision':note['revision']})

def contribution():
    return PortableOwner('language',describe)
