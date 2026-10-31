"""Best-effort channel snapshots through the locked engine, with no feed traversal."""
from io import BytesIO
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from modules.youtube import metadata

HOSTS = {'yt3.ggpht.com', 'yt3.googleusercontent.com'}
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4096 * 4096


def remote_url(value):
    if not isinstance(value, str) or len(value) > 4096 or '\\' in value or any(ord(c) < 33 for c in value):
        raise ValueError('Unsupported profile image URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Unsupported profile image URL')
    return value


class Redirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, request, fp, code, message, headers, newurl):
        remote_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def read_remote(url):
    opener = build_opener(ProxyHandler({}), Redirects())
    with opener.open(Request(remote_url(url), headers={'Accept-Encoding': 'identity'}), timeout=8) as response:
        remote_url(response.url)
        length = response.headers.get('Content-Length')
        if length is not None and not 0 < int(length) <= MAX_BYTES:raise ValueError('Profile image is too large or empty')
        data = response.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES:raise ValueError('Profile image is too large or empty')
    return data


def image_type(data):
    from PIL import Image
    if not 0 < len(data) <= MAX_BYTES:raise ValueError('Profile image is too large or empty')
    try:
        with Image.open(BytesIO(data)) as image:
            name = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}.get(image.format)
            if not name or getattr(image, 'n_frames', 1) != 1 or not 0 < image.width * image.height <= MAX_PIXELS:
                raise ValueError('Unsupported profile image')
            image.verify()
        with Image.open(BytesIO(data)) as image:image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('Unsupported profile image') from error
    return name


def read_preserved(connection, legacy_id, *, suite_home):
    """Read local author artwork without jobs, acquisition or active cutover.

    The supplied connection's current registry authorizes access. Frozen roots
    are evidence only; missing optional artwork has no cover/network fallback.
    """
    import stat
    from files_base import sources, serving
    from modules.youtube import layout
    from services import item_catalog

    captured = metadata.preserved_capture(connection, legacy_id)
    if captured is None:
        return None
    record = captured['payload']['record']
    relative = record.get('avatar', '')
    if not isinstance(relative, str) or not isinstance(record.get('source_id'), str) or not record['source_id']:
        raise ValueError('Invalid preserved profile image facts')
    if not relative:
        return None

    def locate():
        source = sources.get_source(connection, record['source_id'])
        if source is None or not source.visible or source.role not in ('files', 'video', 'youtube'):
            raise ValueError('Registered profile image folder is unavailable')
        library = item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source.source_id)
        if library is None:
            raise ValueError('Saved profile image library is unavailable')
        forbidden = [Path(suite_home), *(Path(child.path) for child in
                     sources.descendant_sources(source, sources.list_sources(connection)))]
        observed = metadata.preserved_media(connection, legacy_id, source_id=source.source_id,
            source_root=source.path, forbidden_roots=forbidden, hash_assets=False)
        if observed is None or observed['record'] != record:
            raise ValueError('Preserved profile image facts changed')
        if observed['assets'].get('avatar') is None:
            return None
        folder = Path(record['path']).parent
        if relative not in {(folder / name).as_posix() for name in metadata.ART_NAMES}:
            raise ValueError('Preserved profile image location changed')
        root = Path(source.path).resolve(strict=True)
        path = layout.resolve(root, relative)
        if serving.resolve_served_file(root, relative, forbidden) != path:
            raise ValueError('Preserved profile image location changed')
        return path, root, library['id']

    located = locate()
    if located is None:
        return None
    path, _, _ = located
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
        raise ValueError('Profile image is too large or empty')
    def identity(value):
        return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    with path.open('rb') as handle:
        if identity(os.fstat(handle.fileno())) != identity(before):
            raise ValueError('Preserved profile image changed during reading')
        data = handle.read(MAX_BYTES + 1)
        if identity(os.fstat(handle.fileno())) != identity(before):
            raise ValueError('Preserved profile image changed during reading')
    extension = image_type(data)
    if path.suffix != '.' + extension:
        raise ValueError('Profile image format changed')
    if locate() != located or identity(path.stat()) != identity(before):
        raise ValueError('Preserved profile image changed during reading')
    return data, {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}[extension]


def lookup(identity, logger):
    from yt_dlp import YoutubeDL

    class ChannelDL(YoutubeDL):
        remaining = 4
        remaining_bytes = 8 * 1024 * 1024

        def urlopen(self, request):
            if self.remaining <= 0:raise ValueError('Channel metadata request limit exceeded')
            self.remaining -= 1
            response = super().urlopen(request)
            owner = self

            class BoundedResponse:
                def __getattr__(self, name):return getattr(response, name)
                def __enter__(self):return self
                def __exit__(self, *args):response.close()
                def read(self, amt=None):
                    maximum = owner.remaining_bytes + 1
                    data = response.read(min(amt, maximum) if amt is not None and amt >= 0 else maximum)
                    owner.remaining_bytes -= len(data)
                    if owner.remaining_bytes < 0:raise ValueError('Channel metadata response limit exceeded')
                    return data

            return BoundedResponse()

    options = dict(skip_download=True, extract_flat=True, lazy_playlist=True, playlist_items='0',
        cachedir=False, socket_timeout=8, retries=0, extractor_retries=0, quiet=True,
        logger=logger, js_runtimes={}, remote_components=set(), proxy='',
        extractor_args={'youtubetab': {'skip': ['authcheck']}})
    with ChannelDL(options) as engine:
        # process=False returns the extractor's lazy result; never inspect its entries.
        result = engine.extract_info(metadata.channel_url(identity) + '/about', download=False, process=False, ie_key='YoutubeTab')
    if not isinstance(result, dict) or result.get('channel_id', result.get('id')) != identity:
        raise ValueError('Channel response does not match the requested author')
    if result.get('id') and result['id'] != identity:
        raise ValueError('Channel response does not match the requested author')
    thumbnails = result.get('thumbnails')
    if not isinstance(thumbnails, list) or len(thumbnails) > 100:raise ValueError('Channel profile image was not supplied')
    # The locked extractor labels author artwork explicitly. Banners/video covers are never candidates.
    avatar = next((item.get('url') for item in thumbnails if isinstance(item, dict) and item.get('id') == 'avatar_uncropped'), None)
    if avatar is None:raise ValueError('Channel profile image was not supplied')
    return remote_url(avatar)


def capture(facts, directory, logger):
    if not facts['channel_id']:return facts
    try:
        directory=Path(directory)
        identity=(directory.stat().st_dev,directory.stat().st_ino)
        if directory.resolve()!=directory:raise ValueError('Profile image directory changed')
        data = read_remote(lookup(facts['channel_id'], logger))
        extension = image_type(data)
        private = directory / '.keivotos'
        if directory.resolve()!=directory or (directory.stat().st_dev,directory.stat().st_ino)!=identity or private.is_symlink():
            raise ValueError('Profile image directory changed')
        private.mkdir(exist_ok=True)
        relative = f'.keivotos/channel-avatar.{extension}'
        with (directory / relative).open('xb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        return {**facts, 'avatar': relative, 'avatar_error': ''}
    except Exception as error:
        # Do not persist expiring image URLs or raw remote exception text.
        reason = 'Channel profile image could not be captured.'
        if isinstance(error, ValueError):reason = str(error) if str(error) in (
            'Channel response does not match the requested author', 'Channel profile image was not supplied',
            'Channel metadata request limit exceeded', 'Channel metadata response limit exceeded', 'Unsupported profile image URL',
            'Profile image is too large or empty', 'Unsupported profile image') else reason
        return {**facts, 'avatar': '', 'avatar_error': reason}
