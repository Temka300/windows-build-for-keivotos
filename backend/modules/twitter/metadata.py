"""Passive exact capture facts for Files; no acquisition or job imports."""
from pathlib import PurePosixPath

from modules.twitter import archive
from files_base import serving


def file_metadata(context):
    relative = context.relative_path
    path = PurePosixPath(relative)
    manifest = relative if path.name == 'manifest.json' else (path.parent / 'manifest.json').as_posix()
    if archive.MANIFEST_PATH.fullmatch(manifest) is None:
        from pathlib import Path
        from storage_layout import portable_path
        try:
            marker=portable_path(context.source.path,(Path(manifest).parent/'.keivotos/item.json').as_posix(),
                archive.forbidden_roots(context.source,context.registered,context.suite_home),must_exist=False)
            if not marker.exists():return None
        except (serving.ServeDenied,OSError,ValueError):return {'status':'unavailable'}
    try:
        item = archive.capture(context.source, context.registered, context.suite_home, manifest)
    except (archive.InvalidCapture, serving.ServeDenied, OSError, ValueError):
        return {'status': 'unavailable'}
    if relative != manifest and path.name not in item['files']:
        return None
    author = item['author']
    def field(label, value, href=None):
        return {'label': label, 'value': value or None, 'href': href}
    profile = 'https://x.com/' + author['handle']
    return {'fields': [field('Source URL', item['source_url'], item['source_url']),
        field('Author URL', profile, profile),
        field('Post ID', item['post_id']), field('Author', author['name']),
        field('Handle', '@' + author['handle']), field('Author ID', author['id']),
        field('Posted', item['posted_at']), field('Captured', item['captured_at']),
        field('Post text', item['raw_text'])],
        'related': [{'label': 'Saved asset', 'path': (path.parent / name).as_posix()}
                    for name in item['files'] if name != 'source.json']}
