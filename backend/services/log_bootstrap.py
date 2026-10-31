"""Early crash diagnostics. Installing hooks creates no profile, files or workers."""
from collections import deque
from datetime import datetime, timezone
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from uuid import uuid4

from services.diagnostic_events import redact_log_text
from services.diagnostic_policy import LogSession, guarded

_RUN_ID = uuid4().hex
_lock = threading.RLock()
_writer = None
_session = None
_early = deque()
_early_bytes = 0
_dropped = 0
_warnings = {}
_installed = False


def fallback_root():
    base = Path(tempfile.gettempdir()).absolute()
    identity = os.environ.get("KEIVOTOS_HOME") or str(Path.home())
    name = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = base / "Keivotos-diagnostics" / name
    guarded(path, base).mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def notice(code, path=None, *, counts=None):
    """Fixed notices only; never dump a failed LogRecord or exception arguments."""
    now = time.monotonic()
    with _lock:
        count, previous = _warnings.get(code, (0, -1000))
        _warnings[code] = (count+1, previous)
        if now-previous < 5:
            return
        _warnings[code] = (count+1, now)
    text = "Keivotos diagnostics: " + code
    if path is not None:
        text += "; log: " + redact_log_text(str(path))
    if counts:
        text += "; " + " ".join(key+"="+str(value) for key,value in counts.items() if key in ("retained","dropped") and type(value) is int)
    if count:
        text += "; repeated notices=" + str(count)
    try:
        sys.stderr.write(text + "\n");sys.stderr.flush()
    except Exception:
        pass


def state():
    return {"startup_buffered": len(_early), "startup_dropped": _dropped,
            "suppressed_notices": sum(max(0, count-1) for count, _ in _warnings.values())}


def record_failure(code, exception=None):
    global _writer, _session, _early_bytes, _dropped
    text = datetime.now(timezone.utc).isoformat(timespec="seconds")+" ERROR "+code
    if exception is not None:
        try:
            text += "\n"+"".join(traceback.TracebackException.from_exception(
                exception, limit=24, capture_locals=False).format())[:12000]
        except Exception:
            text += " [exception details unavailable]"
    text = redact_log_text(text)
    with _lock:
        try:
            if _writer is None:
                root = fallback_root()
                path = root / _RUN_ID / "startup.log"
                if _session is None:
                    _session = LogSession(root, _RUN_ID, [path],budget_bytes=16*1024*1024)
                _session.cleanup()
                _writer = RotatingFileHandler(path, maxBytes=1024*1024, backupCount=2, encoding="utf-8", delay=True)
                _writer.setFormatter(logging.Formatter("%(message)s"))
            # Write directly so stdlib's raw error handler cannot print arguments.
            lines = [*_early, text]
            for line in lines:
                if not _session.allow(len(line.encode("utf-8"))+1):
                    raise OSError("Startup diagnostic budget unavailable")
                record = logging.makeLogRecord({"msg": line, "args": (), "levelno": logging.ERROR})
                if _writer.shouldRollover(record):
                    _writer.doRollover()
                if _writer.stream is None:
                    _writer.stream = _writer._open()
                _writer.stream.write(line+"\n");_writer.stream.flush()
            _early.clear();_early_bytes=0
            notice("startup_failure", _writer.baseFilename)
            return str(_writer.baseFilename)
        except Exception:
            size=len(text.encode("utf-8"))
            while _early and (len(_early)>=256 or _early_bytes+size>1024*1024):
                removed=_early.popleft();_early_bytes-=len(removed.encode("utf-8"));_dropped+=1
            if size<=1024*1024:
                _early.append(text);_early_bytes+=size
            else:
                _dropped+=1
            notice("startup_storage_unavailable")
            return None


def install():
    global _installed
    if _installed:
        return
    _installed=True
    def fatal(kind,value,tb):
        if issubclass(kind,KeyboardInterrupt):
            sys.__excepthook__(kind,value,tb);return
        record_failure("startup.unhandled_exception",value)
    def thread(arguments):
        if arguments.exc_type is not SystemExit:
            record_failure("startup.unhandled_thread_exception",arguments.exc_value)
    sys.excepthook=fatal
    threading.excepthook=thread
