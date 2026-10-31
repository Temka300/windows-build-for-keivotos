"""Verified publication facts for the installed tag; no updater or startup I/O."""
from __future__ import annotations

from datetime import datetime
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import Lock
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "Temka300/Keivotos"
MAX_BYTES = 64 * 1024
TIMEOUT_SECONDS = 5
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")
log = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _publication(value: object, tag: str) -> str | None:
    if not isinstance(value, dict) or value.get("tag_name") != tag or value.get("draft") is not False:
        return None
    timestamp = value.get("published_at")
    if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
        return None
    try:
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            return None
    except ValueError:
        return None
    return timestamp


class ReleaseMetadata:
    """One bounded lookup per process/version, with a create-only local cache."""

    def __init__(self, version: str, home: Path, cache_directory: Path):
        self.version = version
        self.tag = "v" + version
        self.home = home
        self.cache_directory = cache_directory
        self._lock = Lock()
        self._checked = False
        self._published_at: str | None = None

    def _directory(self, *, create: bool = False) -> Path:
        home = self.home.resolve()
        relative = self.cache_directory.absolute().relative_to(self.home.absolute())
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            raise ValueError("Unsafe release cache path")
        current = self.home
        for part in relative.parts:
            current = current / part
            if create:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            if current.is_symlink() or not current.is_dir() or not current.resolve().is_relative_to(home):
                raise ValueError("Unsafe release cache directory")
        return current

    def _read_cache(self) -> str | None:
        try:
            path = self._directory() / (self.tag + ".json")
            if path.is_symlink():
                return None
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    return None
                payload = json.loads(handle.read(MAX_BYTES + 1))
            if not isinstance(payload, dict) or payload.get("schema") != 1 or payload.get("repository") != REPOSITORY:
                return None
            return _publication(payload, self.tag)
        except (OSError, ValueError, TypeError):
            return None

    def _save_cache(self, published_at: str) -> None:
        temporary: Path | None = None
        try:
            directory = self._directory(create=True)
            payload = {"schema": 1, "repository": REPOSITORY, "tag_name": self.tag,
                       "draft": False, "published_at": published_at}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".publication-", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            # Publish complete bytes without replacing an existing file or link.
            # If hard links are unavailable, this process still retains the fact.
            os.link(temporary, directory / (self.tag + ".json"))
        except (OSError, ValueError):
            log.debug("Release publication cache unavailable")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.debug("Release publication cache temporary file retained")

    def _fetch(self) -> object:
        url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{self.tag}"
        request = Request(url, headers={"Accept": "application/vnd.github+json",
            "User-Agent": f"Keivotos/{self.version}", "X-GitHub-Api-Version": "2026-03-10"})
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200 or response.geturl() != url:
                return None
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return None
            return json.loads(body)

    def read(self) -> dict:
        with self._lock:
            if not self._checked:
                self._checked = True
                if re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", self.version):
                    self._published_at = self._read_cache()
                    if self._published_at is None:
                        try:
                            self._published_at = _publication(self._fetch(), self.tag)
                        except (OSError, ValueError, TypeError, HTTPException):
                            log.debug("Release publication lookup unavailable")
                        if self._published_at is not None:
                            self._save_cache(self._published_at)
            return {"version": self.version, "tag_name": self.tag, "published_at": self._published_at}


_reader: ReleaseMetadata | None = None
_reader_lock = Lock()


def installed_release() -> dict:
    global _reader
    with _reader_lock:
        if _reader is None:
            from config import RELEASE_METADATA_CACHE_DIR, SUITE_HOME
            from product import VERSION
            _reader = ReleaseMetadata(VERSION, SUITE_HOME, RELEASE_METADATA_CACHE_DIR)
        reader = _reader
    return reader.read()
