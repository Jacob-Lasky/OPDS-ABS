"""Regression tests for the second-opinion review (Codex, 2026-10-16)."""
import asyncio
import logging
import threading
import time

from lxml import etree

from conftest import ALICE


def test_cache_key_depends_on_token():
    """Keyed on username alone, a re-created account inherited the old one's catalog."""
    from opds_abs.utils.cache_utils import _create_cache_key
    assert _create_cache_key("/x", None, "alice", "t1") != _create_cache_key("/x", None, "alice", "t2")


def test_malformed_authorization_header_is_not_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.get("/opds/alice", headers={"Authorization": "Bearerabc123secret"})
    assert "abc123secret" not in "\n".join(r.getMessage() for r in caplog.records)


def test_content_disposition_keeps_semicolons_in_quoted_name():
    from opds_abs.main import _content_disposition
    v = _content_disposition('attachment; filename="foo;bar.epub"', "x.epub")
    assert 'filename="foo;bar.epub"' in v and "filename*=UTF-8''foo%3Bbar.epub" in v


def test_content_disposition_strips_crlf():
    from opds_abs.main import _content_disposition
    v = _content_disposition('attachment; filename="a\r\nX-Evil: 1.epub"', "x.epub")
    assert "\r" not in v and "\n" not in v


def test_unhandled_error_keeps_request_id(client, monkeypatch):
    from opds_abs import main

    async def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(main.library_feed, "generate_root_feed", boom)
    r = client.get("/opds/alice", auth=ALICE)
    assert r.status_code == 500
    assert etree.fromstring(r.content).findtext("{*}id") == r.headers["x-request-id"]
    assert "synthetic" not in r.text


def test_download_closes_session_when_cancelled_before_headers(monkeypatch):
    import aiohttp
    from opds_abs import main

    closed = []

    async def cancelled_get(self, *a, **k):
        raise asyncio.CancelledError

    orig_close = aiohttp.ClientSession.close

    async def spy_close(self):
        closed.append(True)
        await orig_close(self)
    monkeypatch.setattr(aiohttp.ClientSession, "get", cancelled_get)
    monkeypatch.setattr(aiohttp.ClientSession, "close", spy_close)

    async def run():
        try:
            await main.proxy_download("item-1", "ino-1", ("alice", "tok-alice", "alice"))
        except asyncio.CancelledError:
            return
        raise AssertionError("expected cancellation to propagate")
    asyncio.run(run())
    assert closed == [True]


def test_slow_save_cannot_overwrite_a_later_clear(tmp_path, monkeypatch):
    """A background save that snapshotted before /admin/cache/clear must not land after it."""
    from opds_abs.utils import cache_utils
    monkeypatch.setattr(cache_utils, "CACHE_FILE_PATH", str(tmp_path / "cache.pkl"))
    monkeypatch.setattr(cache_utils, "CACHE_PERSISTENCE_ENABLED", True)
    cache_utils._cache.clear()
    cache_utils.cache_set("stale", 1)

    # Stall the background save in the race window: after it has snapshotted the
    # cache, before it writes. The clear then runs to completion first.
    import pathlib
    first = threading.Event()

    def slow_path(*a):
        if threading.current_thread() is not threading.main_thread() and not first.is_set():
            first.set()
            time.sleep(0.3)
        return pathlib.Path(*a)
    monkeypatch.setattr(cache_utils, "Path", slow_path)

    t = threading.Thread(target=cache_utils.save_cache_to_disk, args=(True,))
    t.start()
    first.wait(2)
    cache_utils.clear_cache()
    t.join()
    cache_utils.load_cache_from_disk()
    assert "stale" not in cache_utils._cache


def test_content_disposition_raw_latin1_from_older_abs():
    """Older ABS sent latin-1 bytes; aiohttp hands them over surrogate-escaped (upstream #42)."""
    from opds_abs.main import _content_disposition
    v = _content_disposition('attachment; filename="M\udcfcller.epub"', "x.epub")
    assert "filename*=UTF-8''M%C3%BCller.epub" in v and 'filename="Muller.epub"' in v


def test_content_disposition_prefers_rfc5987_from_current_abs():
    """ABS 2.37 sends an ASCII fallback with '?' plus filename*; the real name must win."""
    from opds_abs.main import _content_disposition
    v = _content_disposition(
        "attachment; filename=\"Ranger?s.epub\"; filename*=UTF-8''Ranger%E2%80%99s.epub", "x.epub")
    assert "filename*=UTF-8''Ranger%E2%80%99s.epub" in v
