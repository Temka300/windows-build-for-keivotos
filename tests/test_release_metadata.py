"""Release dates are explicit and cannot leak across version changes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("set_version", ROOT / "scripts/release/set_version.py")
set_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_version)


def test_release_date_requires_an_explicit_timezone():
    for value in ("2026-11-02T10:43:17", "not a date", "2026-11-02"):
        with pytest.raises(ValueError, match="timezone|ISO 8601"):
            set_version.release_timestamp(value)
    assert set_version.release_timestamp("2026-11-02T10:43:17+09:00") == "2026-11-02T10:43:17+09:00"
    assert set_version.release_timestamp("2026-11-01T23:43:17-04:00") == "2026-11-01T23:43:17-04:00"


def test_version_change_clears_old_release_date(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "product.py").write_text('VERSION = "1.2.1"\nRELEASED_AT = "2026-09-20T10:00:00+09:00"\n')
    monkeypatch.setattr(set_version, "ROOT", tmp_path)

    assert set_version.recorded_release_timestamp("1.2.2", None) == ""
    assert set_version.recorded_release_timestamp("1.2.1", None) == "2026-09-20T10:00:00+09:00"
    assert set_version.recorded_release_timestamp("1.2.2", "2026-11-02T10:43:17+09:00") == "2026-11-02T10:43:17+09:00"


def test_current_frontend_and_backend_release_identity_agree():
    import sys

    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from product import RELEASED_AT, VERSION
    finally:
        sys.path.pop(0)
    frontend = (ROOT / "frontend/src/lib/product.ts").read_text(encoding="utf-8")
    assert f"export const VERSION = '{VERSION}';" in frontend
    assert f"export const RELEASED_AT = '{RELEASED_AT}';" in frontend


# Publication lookup tests use disposable profiles and never contact GitHub.
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(ROOT / "backend"))
from services import release_metadata as metadata


@pytest.fixture(autouse=True)
def no_release_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real release network is forbidden in regressions")
    monkeypatch.setattr(metadata, "build_opener", denied)


@pytest.fixture
def publication():
    return {"tag_name": "v2.3.4", "draft": False, "prerelease": False,
            "published_at": "2027-01-15T15:30:00Z", "created_at": "2026-10-21T00:00:00Z"}


@pytest.fixture
def reader(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return metadata.ReleaseMetadata("2.3.4", home, home / "cache" / "releases")


def cache_path(reader):
    return reader.cache_directory / (reader.tag + ".json")


def test_exact_publication_is_cached_for_offline_restart(reader, publication, monkeypatch):
    fetch = Mock(return_value=publication)
    monkeypatch.setattr(reader, "_fetch", fetch)
    first = reader.read()
    assert first == {"version": "2.3.4", "tag_name": "v2.3.4", "published_at": publication["published_at"]}
    assert reader.read() == first
    assert fetch.call_count == 1
    cached = json.loads(cache_path(reader).read_bytes())
    assert cached == {"schema": 1, "repository": metadata.REPOSITORY, "tag_name": "v2.3.4",
                      "draft": False, "published_at": publication["published_at"]}
    assert cache_path(reader).stat().st_nlink == 1
    restarted = metadata.ReleaseMetadata(reader.version, reader.home, reader.cache_directory)
    monkeypatch.setattr(restarted, "_fetch", Mock(side_effect=AssertionError("Cache must be offline")))
    assert restarted.read() == first
    assert list(reader.cache_directory.iterdir()) == [cache_path(reader)]


@pytest.mark.parametrize("change", [
    {"tag_name": "v9.9.9"}, {"tag_name": "2.3.4"}, {"draft": True}, {"draft": None},
    {"draft": 0}, {"published_at": None}, {"published_at": ""},
    {"published_at": "2027-01-15"}, {"published_at": "2027-01-15T12:00:00"},
    {"published_at": "2027-02-29T12:00:00Z"}, {"published_at": "2027-01-15T24:00:00Z"},
    {"published_at": "2027-01-15T12:00:00+24:00"},
    {"published_at": "2027-01-15T12:00:00+00:60"}, {"published_at": "2027-01-15T12:00:00-09:99"}, {"published_at": "0000-01-01T00:00:00Z"},
])
def test_wrong_or_unpublished_facts_remain_absent(reader, publication, monkeypatch, change):
    monkeypatch.setattr(reader, "_fetch", Mock(return_value={**publication, **change}))
    assert reader.read()["published_at"] is None
    assert not cache_path(reader).exists()


@pytest.mark.parametrize("body", [None, [], "bad", 123, {"created_at": "2027-01-15T12:00:00Z"}])
def test_unrecognized_payload_is_not_a_release(reader, monkeypatch, body):
    monkeypatch.setattr(reader, "_fetch", Mock(return_value=body))
    assert reader.read()["published_at"] is None


def test_published_prerelease_for_exact_installed_tag_is_valid(reader, publication, monkeypatch):
    monkeypatch.setattr(reader, "_fetch", Mock(return_value={**publication, "prerelease": True}))
    assert reader.read()["published_at"] == publication["published_at"]


@pytest.mark.parametrize("error", [
    TimeoutError(), URLError("offline"), HTTPError("fixture", 404, "missing", {}, None),
    HTTPError("fixture", 403, "limit", {"Retry-After": "3600"}, None),
    HTTPError("fixture", 429, "limit", {}, None), IncompleteRead(b"partial"), ValueError("invalid JSON"),
])
def test_failures_are_absent_and_not_retried(reader, monkeypatch, error):
    fetch = Mock(side_effect=error)
    monkeypatch.setattr(reader, "_fetch", fetch)
    for _ in range(3):
        assert reader.read()["published_at"] is None
    assert fetch.call_count == 1
    assert not cache_path(reader).exists()


def test_concurrent_reads_share_one_lookup(reader, publication, monkeypatch):
    fetch = Mock(return_value=publication)
    monkeypatch.setattr(reader, "_fetch", fetch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: reader.read(), range(24)))
    assert all(value == values[0] for value in values)
    assert fetch.call_count == 1


@pytest.mark.parametrize("content", [b"not JSON", b"[]", b"{}", b"x" * (metadata.MAX_BYTES + 1),
    b'{"schema":1,"repository":"another/repo","draft":false,"tag_name":"v2.3.4","published_at":"2027-01-15T15:30:00Z"}',
    b'{"schema":1,"repository":"Temka300/Keivotos","draft":false,"tag_name":"v9.9.9","published_at":"2027-01-15T15:30:00Z"}',
], ids=["malformed", "list", "missing", "oversized", "wrong-repository", "wrong-tag"])
def test_unrecognized_cache_is_ignored_and_preserved(reader, publication, monkeypatch, content):
    reader.cache_directory.mkdir(parents=True)
    path = cache_path(reader)
    path.write_bytes(content)
    monkeypatch.setattr(reader, "_fetch", Mock(return_value=publication))
    assert reader.read()["published_at"] == publication["published_at"]
    assert path.read_bytes() == content
    assert list(reader.cache_directory.iterdir()) == [path]


def test_different_versions_cannot_reuse_each_others_cache(reader, publication, monkeypatch):
    monkeypatch.setattr(reader, "_fetch", Mock(return_value=publication))
    reader.read()
    original = cache_path(reader).read_bytes()
    newer = metadata.ReleaseMetadata("2.3.5", reader.home, reader.cache_directory)
    monkeypatch.setattr(newer, "_fetch", Mock(side_effect=URLError("offline")))
    assert newer.read()["published_at"] is None
    assert cache_path(reader).read_bytes() == original


@pytest.mark.parametrize("kind", ["directory", "file", "hardlink", "outside", "traversal"])
def test_unsafe_cache_paths_never_read_or_rewrite_outside(reader, publication, monkeypatch, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "preserve.json"
    original = json.dumps({"schema": 1, "repository": metadata.REPOSITORY, **publication}).encode()
    target.write_bytes(original)
    try:
        if kind == "directory":
            (reader.home / "cache").symlink_to(outside, target_is_directory=True)
        elif kind in ("file", "hardlink"):
            reader.cache_directory.mkdir(parents=True)
            if kind == "file": cache_path(reader).symlink_to(target)
            else: os.link(target, cache_path(reader))
        elif kind == "outside":
            reader.cache_directory = outside
        else:
            reader.cache_directory = reader.home / ".." / "outside"
    except OSError as exc:
        pytest.skip(f"Link fixture unavailable: {type(exc).__name__}")
    # Offline: a forged cache through a link/outside path must not be accepted.
    fetch = Mock(side_effect=URLError("offline"))
    monkeypatch.setattr(reader, "_fetch", fetch)
    assert reader.read()["published_at"] is None
    # Online: display a separately verified fact, but never mutate that target.
    fresh = metadata.ReleaseMetadata(reader.version, reader.home, reader.cache_directory)
    monkeypatch.setattr(fresh, "_fetch", Mock(return_value=publication))
    assert fresh.read()["published_at"] == publication["published_at"]
    assert target.read_bytes() == original
    assert list(outside.iterdir()) == [target]


def test_cache_permission_failure_keeps_verified_fact_in_memory(reader, publication, monkeypatch):
    monkeypatch.setattr(reader, "_fetch", Mock(return_value=publication))
    monkeypatch.setattr(metadata.os, "link", Mock(side_effect=PermissionError("fixture")))
    assert reader.read()["published_at"] == publication["published_at"]
    assert not cache_path(reader).exists()
    assert not list(reader.cache_directory.iterdir())


@pytest.mark.parametrize("version", ["../escape", "2.3.4/other", "", "https://other", "2.3"])
def test_unrecognized_identity_cannot_construct_a_network_or_cache_path(reader, monkeypatch, version):
    reader.version = version
    fetch = Mock(side_effect=AssertionError("Invalid identity must not be fetched"))
    monkeypatch.setattr(reader, "_fetch", fetch)
    assert reader.read()["published_at"] is None
    fetch.assert_not_called()


def install_response(monkeypatch, reader, body, *, status=200, url=None):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status = status
    response.geturl.return_value = url or f"https://api.github.com/repos/{metadata.REPOSITORY}/releases/tags/{reader.tag}"
    response.read.side_effect = lambda limit: body[:limit]
    opener = Mock()
    opener.open.return_value = response
    create = Mock(return_value=opener)
    monkeypatch.setattr(metadata, "build_opener", create)
    return opener, response, create


def test_http_read_uses_exact_tag_no_credentials_and_bounded_response(reader, publication, monkeypatch):
    opener, response, create = install_response(monkeypatch, reader, json.dumps(publication).encode())
    assert reader.read()["published_at"] == publication["published_at"]
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.github.com/repos/Temka300/Keivotos/releases/tags/v2.3.4"
    assert "/latest" not in request.full_url
    assert opener.open.call_args.kwargs == {"timeout": metadata.TIMEOUT_SECONDS}
    assert request.get_method() == "GET"
    assert set(dict(request.header_items())) == {"Accept", "User-agent", "X-github-api-version"}
    response.read.assert_called_once_with(metadata.MAX_BYTES + 1)
    assert isinstance(create.call_args.args[0], metadata._NoRedirect)
    assert create.call_args.args[0].redirect_request(None, None, 302, "redirect", {}, "https://other") is None


@pytest.mark.parametrize("body,status,url", [(b"{}", 202, None), (b"invalid", 200, None),
    (b"x" * (metadata.MAX_BYTES + 1), 200, None), (b"{}", 200, "https://other.invalid")],
    ids=["wrong-status", "malformed", "oversized", "redirect"])
def test_http_invalid_responses_are_not_cached(reader, monkeypatch, body, status, url):
    install_response(monkeypatch, reader, body, status=status, url=url)
    assert reader.read()["published_at"] is None
    assert not cache_path(reader).exists()


def test_suite_endpoint_exposes_only_installed_publication(monkeypatch, publication):
    from fastapi import FastAPI
    from routers import suite
    from tests.test_operation_diagnostics import LocalClient
    result = {"version": "2.3.4", "tag_name": "v2.3.4", "published_at": publication["published_at"]}
    monkeypatch.setattr(metadata, "installed_release", Mock(return_value=result))
    app = FastAPI()
    app.include_router(suite.router)
    async def exercise():
        async with LocalClient(app) as client:
            response = await client.get("/api/suite/release")
        assert response.status_code == 200
        assert response.json() == result
    asyncio.run(exercise())
