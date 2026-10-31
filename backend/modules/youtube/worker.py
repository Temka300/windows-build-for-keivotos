"""Dedicated yt-dlp process: structured events, no app DB or shell command input."""
from services.operation_diagnostics import worker_diagnostics
import json
import sys
import time
import shlex
from services.tool_diagnostics import command_details
from pathlib import Path
from modules.youtube.engine import canonical_url, format_selector
from modules.youtube import metadata, channel_art, details


def emit(**event):
    print(json.dumps(event,ensure_ascii=True),flush=True)


class Log:
    def debug(self,message):
        if message.startswith('[debug] ffmpeg command line: '):
            try:
                command=shlex.split(message.split(': ',1)[1])
                emit(event='diagnostic',code='ffmpeg',facts=command_details(command))
            except ValueError:
                emit(event='diagnostic',code='ffmpeg',facts={'tool_version':'unavailable'})
        elif not message.startswith('[debug]'):print(message,file=sys.stderr,flush=True)
    def warning(self,message):print(message,file=sys.stderr,flush=True)
    def error(self,message):print(message,file=sys.stderr,flush=True)



class TransferProgress:
    """Combine yt-dlp's selected track sizes and observed bytes, without guessing."""
    def __init__(self):
        self.totals={}
        self.downloaded={}

    @staticmethod
    def amount(value):
        import math
        return float(value) if type(value) in (int,float) and math.isfinite(value) and value>=0 else None

    def plan(self,info):
        selected=info.get('requested_formats') or [info]
        if not isinstance(selected,list) or len(selected)>32:return
        for track in selected:
            key=str(track.get('format_id') or 'media')
            total=self.amount(track.get('filesize')) or self.amount(track.get('filesize_approx'))
            if key not in self.totals:self.totals[key]=total

    def observe(self,data):
        info=data.get('info_dict') or {}
        if not self.totals:self.plan(info)
        total=self.amount(data.get('total_bytes')) or self.amount(data.get('total_bytes_estimate'))
        received=self.amount(data.get('downloaded_bytes'))
        # External FFmpeg downloaders can report one combined transfer.
        if info.get('requested_formats'):
            return min(99,received/total*100) if total and received is not None else None
        key=str(info.get('format_id') or 'media')
        if key not in self.totals:return None
        if total:self.totals[key]=total
        if received is not None:self.downloaded[key]=max(self.downloaded.get(key,0),received)
        if data.get('status')=='finished' and received is not None:
            self.totals[key]=received or self.totals[key]
        if not self.totals or not all(self.totals.values()):return None
        return min(99,sum(min(self.downloaded.get(key,0),size) for key,size in self.totals.items())/
                   sum(self.totals.values())*100)

def download(spec):
    from yt_dlp import YoutubeDL
    directory=Path(spec['directory']).resolve()
    moved=[]
    last_progress=0.0
    title=""
    transfer=TransferProgress()
    def match(info,**options):
        nonlocal title
        if info.get('is_live'):return 'Live streams are not supported.'
        observed=info.get('title')
        if isinstance(observed,str) and observed.strip():
            observed=observed.replace('\0','')[:512]
            if observed!=title:title=observed;emit(event='metadata',title=title)
        if not options.get('incomplete') and (info.get('requested_formats') or info.get('format_id')):transfer.plan(info)
        return None
    def progress(data):
        nonlocal last_progress,title
        measured=transfer.observe(data)
        info=data.get('info_dict') or {}
        observed=info.get('title')
        if isinstance(observed,str) and observed.strip():title=observed.replace('\0','')[:512]
        now=time.monotonic()
        if now-last_progress<.2 and data.get("status")!="finished":return
        last_progress=now
        emit(event='progress',progress=measured*.95 if measured is not None else 0,title=title)
        files=[]
        for name in (data.get('filename'),data.get('tmpfilename')):
            if name:
                target=Path(name)
                if target.parent.resolve()!=directory:raise ValueError('Unexpected transfer path')
                files.append(target.name)
        artwork=next((p.name for p in directory.iterdir() if p.stem in ('video','audio') and p.suffix.lower() in ('.jpg','.jpeg','.png','.webp')), '')
        emit(event='file',name=title or Path(data.get('filename') or 'video').name,
             progress=measured,files=list(dict.fromkeys(files)),artwork=artwork)
    def moved_file(filename):
        moved.append(filename)
        media=Path(filename)
        if media.parent.resolve()!=directory:raise ValueError('Unexpected transfer output')
        emit(event='file',name=title or media.stem,progress=99,files=[media.name])
    options={
        'format':format_selector(spec['video'],spec['audio']),
        'outtmpl':str(directory/('audio.%(ext)s' if spec['video']=='audio' else 'video.%(ext)s')),
        'windowsfilenames':True,'noplaylist':True,'overwrites':False,
        'writethumbnail':True,'writedescription':True,'writeinfojson':False,'cachedir':False,
        'socket_timeout':20,'retries':3,'fragment_retries':3,'concurrent_fragment_downloads':1,
        'ffmpeg_location':spec['ffmpeg'],'js_runtimes':{},
        'remote_components':set(),'verbose':True,'quiet':True,'no_warnings':False,'logger':Log(),
        'progress_hooks':[progress], 'post_hooks':[moved_file],
        'postprocessor_hooks':[lambda _:emit(event='merging')],
        'match_filter':match,
    }
    with YoutubeDL(options) as ydl:
        info=ydl.extract_info(canonical_url(spec['url']),download=True)
    if not info or not moved:raise RuntimeError('No completed media output')
    media=Path(moved[-1]).resolve()
    if media.parent!=directory or not media.is_file():raise RuntimeError('Unexpected output path')
    thumbnail=next((p.name for p in directory.iterdir() if p.suffix.lower() in ('.jpg','.jpeg','.png','.webp')), '')
    facts=metadata.capture(info,spec['url'],media)
    facts=channel_art.capture(facts,directory,Log())
    from yt_dlp.version import __version__
    snapshot=details.capture(info,directory,facts,engine_version=__version__)
    emit(event='complete',path=media.name,title=str(info.get('title') or media.stem)[:512],thumbnail=thumbnail,capture=facts,details=snapshot)


@worker_diagnostics
def main(arguments):
    if arguments: return 2
    try:
        spec=json.loads(sys.stdin.readline(16384))
        download(spec)
        return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr,flush=True)
        return 1


if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
