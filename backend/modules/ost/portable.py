"""Explicit track export preserves publisher names, tags, covers and context."""
from services.item_manifest import PortableOwner, local_description
from files_base import index
from modules.ost import library
from services import audio_metadata

def describe(context,selection,options):
    target=context.resolve(selection)
    if target.suffix.lower().lstrip('.') not in library.AUDIO_EXTENSIONS:
        raise ValueError('Select a supported audio track')
    facts=audio_metadata.read(context.index_path,context.source,selection,context.forbidden)
    with index.open_index(context.index_path) as db:
        covers=library.named_covers(db,context.source,'' if target.parent==context.resolve('') else target.parent.relative_to(context.source.path).as_posix(),context.forbidden)
    paths=[(selection,'media'),*[(p.relative_to(context.source.path).as_posix(),'cover') for p in covers]]
    return local_description(context,selection,options,owner='ost',item_type='track',
                             facts={'audio':facts},paths=paths)

def contribution():
    return PortableOwner('ost',describe)
