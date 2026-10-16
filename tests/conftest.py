"""Test harness: a fake Audiobookshelf on a real socket, and the app under test.

The fake ABS runs in a background thread so the app's aiohttp client talks to it
over HTTP exactly as it talks to the real server. Environment variables are set
before ``opds_abs`` is imported because its config is read at import time.
"""
import asyncio
import os
import socket
import threading

import pytest
from aiohttp import web


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ABS_PORT = _free_port()
os.environ.update({
    "AUDIOBOOKSHELF_URL": f"http://127.0.0.1:{ABS_PORT}",
    "AUTH_ENABLED": "true",
    "API_KEY_AUTH_ENABLED": "false",
    "CACHE_PERSISTENCE_ENABLED": "false",
    "OPDS_LOG_LEVEL": "DEBUG",
})

LIB = "lib-1"
USERS = {"alice": "alice-pw", "bob": "bob-pw"}
TOKENS = {f"tok-{u}": u for u in USERS}


def _item(n, title, author):
    return {
        "id": f"item-{n}",
        "media": {
            "ebookFormat": "epub",
            "ebookFile": {"ino": f"ino-{n}", "ebookFormat": "epub"},
            "metadata": {"title": title, "authorName": author, "description": f"About {title}"},
        },
        "libraryFiles": [{"ino": f"ino-{n}", "fileType": "ebook",
                          "metadata": {"filename": f"{title}.epub"}}],
    }


ITEMS = [_item(1, "Harry Potter and the Prisoner of Azkaban", "J.K. Rowling"),
         _item(2, "Dune", "Frank Herbert"),
         _item(3, "Ampersands & Angles <b>", "A. Writer")]


class FakeABS:
    """Records calls so tests can assert what reached Audiobookshelf."""

    def __init__(self):
        self.logins = []
        self.api_calls = []
        self.fail_next = set()  # paths that return HTTP 500 once

    def _user(self, request):
        auth = request.headers.get("Authorization", "")
        return TOKENS.get(auth.removeprefix("Bearer "))

    async def login(self, request):
        body = await request.json()
        self.logins.append(body["username"])
        if body["username"] == "ratelimited":
            return web.Response(status=429, text="Too many requests")
        if USERS.get(body["username"]) != body["password"]:
            return web.Response(status=401, text="Unauthorized")
        u = body["username"]
        return web.json_response({"user": {"username": u, "token": f"tok-{u}", "type": "user"}})

    async def api(self, request):
        self.api_calls.append((request.path, dict(request.query)))
        if not self._user(request):
            return web.Response(status=401, text="Unauthorized")
        path = request.path
        if path in self.fail_next:
            self.fail_next.discard(path)
            return web.Response(status=500, text="transient")
        if path == "/api/libraries":
            return web.json_response({"libraries": [{"id": LIB, "name": "Books", "mediaType": "book"}]})
        if path == f"/api/libraries/{LIB}/items":
            return web.json_response({"results": ITEMS, "total": len(ITEMS)})
        if path == f"/api/libraries/{LIB}/search":
            q = request.query.get("q", "").lower()
            hits = [{"libraryItem": i} for i in ITEMS if q in i["media"]["metadata"]["title"].lower()]
            return web.json_response({"book": hits, "series": [], "authors": []})
        if path.startswith("/api/items/") and path.count("/") == 3:
            item_id = path.rsplit("/", 1)[1]
            for i in ITEMS:
                if i["id"] == item_id:
                    return web.json_response(i)
            return web.Response(status=404)
        if "/file/" in path and path.endswith("/download"):
            if path.split("/")[3] not in {i["id"] for i in ITEMS}:
                return web.Response(status=404)
            return web.Response(body=b"EPUBDATA", content_type="application/epub+zip")
        return web.Response(status=404, text=f"fake ABS has no {path}")


@pytest.fixture(scope="session")
def fake_abs():
    fake = FakeABS()
    app = web.Application()
    app.router.add_post("/login", fake.login)
    app.router.add_route("*", "/api/{tail:.*}", fake.api)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", ABS_PORT).start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield fake
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def client(fake_abs):
    from fastapi.testclient import TestClient

    from opds_abs.main import app
    from opds_abs.utils import auth_utils, cache_utils

    cache_utils._cache.clear()
    auth_utils.clear_token_cache()
    fake_abs.logins.clear()
    fake_abs.api_calls.clear()
    fake_abs.fail_next.clear()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


ALICE = ("alice", "alice-pw")
