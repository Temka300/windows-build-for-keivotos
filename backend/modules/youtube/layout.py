"""Portable names and durable channel assignments for new captured saves."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import unicodedata

import config
from files_base import serving
from modules.youtube import metadata

COLUMNS = {'layout_version':'INTEGER NOT NULL DEFAULT 0',
           'bundle_path':"TEXT NOT NULL DEFAULT ''", 'manifest_sha256':"TEXT NOT NULL DEFAULT ''"}
MAX_PATH = 240
TAIL = '/.keivotos/d-0123456789abcdef.txt'


def units(value):
    return len(str(value).encode('utf-16-le')) // 2


def component(value):
    if (not isinstance(value,str) or not value or value in ('.','..') or value[-1] in ' .'
            or len(value.encode('utf-8'))>240 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]',value)
            or re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',value,re.I)):
        raise ValueError('Unsupported saved folder name.')
    return value


def name(title,suffix,budget):
    from yt_dlp.utils import sanitize_filename
    text=unicodedata.normalize('NFC',sanitize_filename(metadata.text(title),is_id=False)).strip(' .') or 'Saved'
    if re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',text,re.I):text='_'+text
    prefix=''
    for char in text:
        candidate=prefix+char
        if units(candidate+suffix)>budget or len(candidate.encode('utf-8'))>64:break
        prefix=candidate
    if not prefix:raise ValueError('The destination path is too long for a portable save.')
    return component(prefix.rstrip(' .')+suffix)


def resolve(root,relative):
    """Consume the shared containment boundary, additionally refusing case aliases."""
    if not isinstance(relative,str) or PurePosixPath(relative).as_posix()!=relative or relative.startswith('/'):
        raise ValueError('Saved item location changed.')
    cursor=root
    for part in relative.split('/'):
        component(part)
        if cursor.is_dir() and any(p.name.casefold()==part.casefold() and p.name!=part for p in cursor.iterdir()):
            raise ValueError('Saved item has a conflicting case alias.')
        cursor/=part
        if cursor.is_symlink():raise ValueError('Saved item contains a symbolic link.')
    if units(cursor)>MAX_PATH:raise ValueError('The destination path is too long for a portable save.')
    return serving.resolve_within_source(root,relative,[config.SUITE_HOME],must_exist=False)


def assign(db,job,root,facts,title):
    if not re.fullmatch('[a-f0-9]{32}',job['id']):raise ValueError('Invalid saved item identity.')
    suffix=f" [{facts['video_id']}] [{job['id'][:16]}]"
    identity=facts['channel_id']
    row=db.execute('SELECT directory FROM youtube_channel_folders WHERE source_id=? AND channel_id=?',
                   (job['source_id'],identity)).fetchone() if identity else None
    if row:
        channel=row['directory'] if isinstance(row,dict) else row[0]
        if len(channel.split('/'))!=2 or not channel.startswith('YouTube/'):
            raise ValueError('Assigned channel location changed.')
    else:
        channel_suffix=f' [{identity}]' if identity else f" [{job['id'][:16]}]"
        available=MAX_PATH-units(root/'YouTube')-2-units(TAIL)-units(suffix)-units(channel_suffix)
        if available<2:raise ValueError('The destination path is too long for a portable save.')
        budget=units(channel_suffix)+min(48,available//2)
        channel='YouTube/'+name(facts['channel_name'] or 'Channel',channel_suffix,budget) if identity else 'YouTube/Unknown channel'+channel_suffix
        parent=root/'YouTube'
        assigned=[r['directory'] if isinstance(r,dict) else r[0] for r in db.execute(
            'SELECT directory FROM youtube_channel_folders WHERE source_id=?',(job['source_id'],))]
        occupied={p.name.casefold() for p in parent.iterdir()} if parent.is_dir() else set()
        if channel.rsplit('/',1)[-1].casefold() in occupied or channel.casefold() in {p.casefold() for p in assigned}:
            extra=' ['+hashlib.sha256((identity or job['id']).encode()).hexdigest()[:8]+']'
            # A case collision needs a digest. Borrow title space while retaining
            # its complete video/saved-item identities and at least one title char.
            collision_budget=MAX_PATH-units(root/'YouTube')-2-units(TAIL)-units(suffix)-1
            channel='YouTube/'+name(facts['channel_name'] or 'Unknown channel',channel_suffix+extra,collision_budget)
            if channel.rsplit('/',1)[-1].casefold() in occupied or channel.casefold() in {p.casefold() for p in assigned}:
                raise ValueError('Channel folder already exists; files were preserved.')
        resolve(root,channel)
        if identity:db.execute('INSERT INTO youtube_channel_folders(source_id,channel_id,directory) VALUES(?,?,?)',
                                (job['source_id'],identity,channel))
    resolve(root,channel)
    budget=MAX_PATH-units(root/channel)-1-units(TAIL)
    relative=channel+'/'+name(title or 'Saved item',suffix,budget)
    resolve(root,relative+TAIL)
    return relative
