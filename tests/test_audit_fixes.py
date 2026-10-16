"""Regression tests for the 2026-10-16 code audit (Jacob-Lasky/opds-abs#1)."""
import pickle
from urllib.parse import parse_qs, urlparse

from lxml import etree

from conftest import ALICE, LIB


def test_index_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_unknown_library_is_404_with_well_formed_error(client):
    r = client.get("/opds/alice/libraries/nope/items", auth=ALICE)
    assert r.status_code == 404
    err = etree.fromstring(r.content)
    assert err.findtext("{*}id") == r.headers["x-request-id"]


def test_error_message_is_escaped():
    from opds_abs.utils.error_utils import error_response
    r = error_response(500, "user a<b>&x", "abc")
    assert etree.fromstring(r.body).findtext("{*}message") == "user a<b>&x"


def test_admin_endpoints_need_abs_admin(client):
    assert client.get("/admin/cache/stats", auth=ALICE).status_code == 403
    assert client.post("/admin/cache/clear", auth=ALICE).status_code == 403
    assert client.get("/admin/cache/stats").status_code == 401


def test_transient_item_failure_is_not_cached(client, fake_abs):
    fake_abs.fail_next.add("/api/items/item-2")
    first = client.get(f"/opds/alice/libraries/{LIB}/items", auth=ALICE).text
    second = client.get(f"/opds/alice/libraries/{LIB}/items", auth=ALICE).text
    assert "item-2/file" not in first
    assert "item-2/file" in second


def test_pagination_links_keep_filter_encoded(client, monkeypatch):
    from opds_abs.feeds import library_feed
    monkeypatch.setattr(library_feed, "ITEMS_PER_PAGE", 1)
    r = client.get(f"/opds/alice/libraries/{LIB}/items?sort=media.metadata.title&filter=c2VyaWVz%2B%2F%3D",
                   auth=ALICE)
    feed = etree.fromstring(r.content)
    nxt = [l.get("href") for l in feed.iter("{*}link") if l.get("rel") == "next"][0]
    assert parse_qs(urlparse(nxt).query)["filter"] == ["c2VyaWVz+/="]


def test_items_feed_does_not_forward_client_params_to_abs(client, fake_abs):
    client.get(f"/opds/alice/libraries/{LIB}/items?sort=title&token=x&bogus=1", auth=ALICE)
    sent = [q for path, q in fake_abs.api_calls if path == f"/api/libraries/{LIB}/items"]
    assert sent and all("token" not in q and "bogus" not in q for q in sent)


def test_series_sequence_key_handles_missing_and_text():
    from opds_abs.feeds.series_feed import series_sequence_key

    def item(seq):
        return {"media": {"metadata": {"series": {"sequence": seq}}}}
    seqs = ["2", None, "1.5", "Book 3", "", "10"]
    ordered = sorted((item(s) for s in seqs), key=series_sequence_key)
    assert [i["media"]["metadata"]["series"]["sequence"] for i in ordered][:3] == ["1.5", "2", "10"]


def test_co_authored_book_counts_for_each_author():
    from opds_abs.utils.cache_utils import book_authors
    known = [{"id": "a1", "name": "Alice Smith"}, {"id": "a2", "name": "Bob Jones"},
             {"id": "a3", "name": "Smith, Jr."}, {"id": "a4", "name": "Alice"}]
    assert {a["id"] for a in book_authors("Alice Smith, Bob Jones", known)} == {"a1", "a2"}
    assert {a["id"] for a in book_authors("Smith, Jr., Bob Jones", known)} == {"a3", "a2"}
    assert book_authors(None, known) == []


def test_cache_file_from_older_version_is_discarded(tmp_path, monkeypatch):
    from opds_abs.utils import cache_utils
    path = tmp_path / "cache.pkl"
    path.write_bytes(pickle.dumps({"k": (0.0, ("tok-alice", "alice"))}))  # v1 layout
    monkeypatch.setattr(cache_utils, "CACHE_FILE_PATH", str(path))
    monkeypatch.setattr(cache_utils, "CACHE_PERSISTENCE_ENABLED", True)
    cache_utils._cache.clear()
    cache_utils.load_cache_from_disk()
    assert cache_utils._cache == {} and not path.exists()


def test_cache_round_trip_mutates_shared_dict(tmp_path, monkeypatch):
    from opds_abs.utils import cache_utils
    monkeypatch.setattr(cache_utils, "CACHE_FILE_PATH", str(tmp_path / "cache.pkl"))
    monkeypatch.setattr(cache_utils, "CACHE_PERSISTENCE_ENABLED", True)
    shared = cache_utils._cache
    cache_utils.cache_set("k", {"v": 1})
    cache_utils.save_cache_to_disk(force=True)
    shared.clear()
    cache_utils.load_cache_from_disk()
    assert cache_utils._cache is shared and shared["k"][1] == {"v": 1}


def test_content_type_is_opds_catalog(client):
    r = client.get(f"/opds/alice/libraries/{LIB}", auth=ALICE)
    assert r.headers["content-type"].startswith("application/atom+xml;profile=opds-catalog")


def test_book_content_is_html_not_xhtml(client):
    feed = etree.fromstring(client.get(f"/opds/alice/libraries/{LIB}/items", auth=ALICE).content)
    types = {c.get("type") for c in feed.iter("{*}content")}
    assert types == {"html"}
