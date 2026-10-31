"""Pure diagnostic budgets and owned-log cleanup; no profile or database imports."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

MIB = 1024 * 1024
DEFAULTS = {"retention_days": 14, "total_mib": 256, "rotation_mib": 16}
_RUN = re.compile(r"[0-9a-f]{32}\Z")


def policy(values=None):
    result = dict(DEFAULTS)
    if values:
        for key, low, high in (("retention_days", 1, 365), ("total_mib", 16, 4096), ("rotation_mib", 1, 64)):
            value = values.get(key, result[key])
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid diagnostic limit: " + key)
            result[key] = value
    if result["rotation_mib"] > result["total_mib"]:
        raise ValueError("Log rotation must fit within the total budget")
    return result


def guarded(path, root):
    path, root = Path(path), Path(root)
    if root.is_symlink() or not path.absolute().is_relative_to(root.absolute()):
        raise OSError("Unsafe diagnostic location")
    node = path
    while node != root:
        if node.is_symlink():
            raise OSError("Linked diagnostic location")
        node = node.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise OSError("Diagnostic location escaped its root")
    if path.exists():
        facts = path.stat()
        if not (stat.S_ISDIR(facts.st_mode) or stat.S_ISREG(facts.st_mode)) or facts.st_nlink > 1 and path.is_file():
            raise OSError("Unsafe diagnostic file")
    return path


def _lock(path, *, create):
    if not create and (not path.is_file() or path.is_symlink()):
        return None
    handle = path.open("a+b" if create else "r+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0");handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


class LogSession:
    """Markers opt new log streams into cleanup; unknown/legacy files are inert."""
    def __init__(self, root, run_id, files, limits=None, *, budget_bytes=None):
        if not _RUN.fullmatch(run_id):
            raise ValueError("Invalid diagnostic run ID")
        self.root = Path(root).absolute()
        guarded(self.root, self.root).mkdir(parents=True, exist_ok=True)
        self.directory = guarded(self.root / run_id, self.root)
        self.directory.mkdir(exist_ok=True)
        self.limits = policy(limits)
        self.budget_bytes=self.limits["total_mib"]*MIB if budget_bytes is None else max(0,min(budget_bytes,self.limits["total_mib"]*MIB))
        self.lock = _lock(guarded(self.directory / "writer.lock", self.root), create=True)
        if self.lock is None:
            raise OSError("Diagnostic session is already active")
        self._mutex=threading.RLock()
        self._checked, self._used = 0.0, 0
        try:
            names = [guarded(Path(name).absolute(), self.root).relative_to(self.root).as_posix() for name in files]
            marker = guarded(self.directory / "managed.json", self.root)
            if marker.exists():
                old = json.loads(marker.read_text())
                if old.get("files") != names:
                    raise OSError("Diagnostic session ownership changed")
            else:
                with marker.open("x", encoding="utf-8") as output:
                    json.dump({"version": 1, "files": names}, output)
        except BaseException:
            self.close()
            raise

    def close(self):
        handle, self.lock = self.lock, None
        if handle is not None:
            handle.close()

    def _streams(self, directory):
        marker = guarded(directory / "managed.json", self.root)
        if not marker.is_file() or marker.stat().st_size > 16384:
            raise OSError("Missing or oversized log ownership marker")
        value = json.loads(marker.read_text())
        if value.get("version") != 1 or not isinstance(value.get("files"), list) or not 1 <= len(value["files"]) <= 8:
            raise ValueError("Unknown log ownership marker")
        result = []
        for name in value["files"]:
            if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
                raise OSError("Invalid managed log")
            base = guarded(self.root / name, self.root)
            valid_name=base.name in ("events-0001.jsonl","startup.log") and base.parent==directory
            valid_name=valid_name or bool(re.fullmatch(r"[a-z0-9]+-(?:runtime|access)-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-p\d+\.log",base.name))
            if not valid_name or base.parent not in (self.root, directory):
                raise OSError("Invalid managed log")
            candidates = [base, *base.parent.glob(base.name + ".*")]
            for path in candidates:
                if path != base and not path.name.removeprefix(base.name + ".").isdigit():
                    continue
                guarded(path, self.root)
                if path.is_file():
                    result.append(path)
        return result

    def cleanup(self, *, now=None):
        with self._mutex:
            return self._cleanup(now=now)

    def _cleanup(self, *, now=None):
        now = time.time() if now is None else now
        groups, total, skipped = [], 0, 0
        for directory in self.root.iterdir():
            if not _RUN.fullmatch(directory.name) or not directory.is_dir():
                continue
            try:
                guarded(directory, self.root)
                files = self._streams(directory)
                facts = [(path, path.stat()) for path in files]
                size = sum(item.st_size for _, item in facts)
                total += size
                newest = max((item.st_mtime for _, item in facts), default=(directory / "managed.json").stat().st_mtime)
                groups.append((newest, directory, facts, size))
            except (OSError, ValueError, TypeError, KeyError):
                skipped += 1
        removed = removed_bytes = 0
        budget = self.budget_bytes
        for newest, directory, facts, size in sorted(groups):
            if newest >= now - self.limits["retention_days"] * 86400 and total <= budget:
                continue
            if directory == self.directory:
                continue
            lock = _lock(guarded(directory / "writer.lock", self.root), create=False)
            if lock is None:
                continue
            try:
                # Recheck identity/time/size after taking the writer's lease.
                for path, before in facts:
                    guarded(path, self.root)
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise OSError("Diagnostic file changed")
                for path, before in facts:
                    path.unlink()
                    removed += 1;removed_bytes += before.st_size;total -= before.st_size
            except OSError:
                skipped += 1
            finally:
                lock.close()
            # Remove only the two owned controls of a now-empty inactive run.
            try:
                if {p.name for p in directory.iterdir()}=={"managed.json","writer.lock"} and not self._streams(directory):
                    for name in ("managed.json","writer.lock"):
                        guarded(directory/name,self.root).unlink()
                    directory.rmdir()
            except (OSError,ValueError):
                skipped+=1
        self._checked, self._used = time.monotonic(), total
        return {"removed_files": removed, "removed_bytes": removed_bytes, "managed_bytes": total,
                "protected_or_invalid": skipped, "budget_blocked": total >= budget}

    def allow(self, size):
        with self._mutex:
            return self._allow(size)

    def _allow(self, size):
        if time.monotonic() - self._checked >= 1 or self._used + size > self.budget_bytes:
            self.cleanup()
        if self._used + size > self.budget_bytes:
            return False
        self._used += size
        return True


_debug_deadline = 0.0
_debug_started = 0.0


def debug_session(active=None):
    global _debug_deadline, _debug_started
    if active is True:
        _debug_started, _debug_deadline = time.monotonic() + 1800, time.time() + 1800
    elif active is False:
        _debug_started = _debug_deadline = 0.0
    enabled = bool(_debug_deadline > time.time() and _debug_started > time.monotonic())
    return {"active": enabled, "expires_at": _debug_deadline if enabled else None,
            "remaining_seconds": max(0, int(min(_debug_deadline-time.time(), _debug_started-time.monotonic()))) if enabled else 0}
