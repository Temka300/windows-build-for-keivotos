"""Source/frozen tool lookup and dispatch without loading app or personal data."""
from pathlib import Path
import shutil
import sys


def ffmpeg_executable():
    if getattr(sys, 'frozen', False):
        name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
        bundled = Path(sys.executable).resolve().parent / name
        return str(bundled) if bundled.is_file() else None
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None


def dispatch_helper(arguments, **_):
    if arguments[:1] != ['--twitter-worker']:
        return None
    from modules.twitter.worker import main
    return main(arguments[1:])
