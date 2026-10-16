from conftest import ALICE


def test_download_requires_auth(client):
    assert client.get("/opds/proxy/download/item-1/file/ino-1").status_code == 401


def test_download_streams_file(client):
    r = client.get("/opds/proxy/download/item-1/file/ino-1", auth=ALICE)
    assert r.status_code == 200 and r.content == b"EPUBDATA"
    assert r.headers["content-type"] == "application/epub+zip"


def test_download_upstream_404_is_404_not_empty_200(client):
    r = client.get("/opds/proxy/download/nope/file/nope", auth=ALICE)
    assert r.status_code == 404


def test_download_rejects_path_tricks(client):
    assert client.get("/opds/proxy/download/..%2F..%2Flogin/file/x", auth=ALICE).status_code == 404


def test_content_disposition_non_ascii():
    from opds_abs.main import _content_disposition
    v = _content_disposition('attachment; filename="Märchen.epub"', "x.epub")
    v.encode("latin-1")  # must be sendable as an HTTP header
    assert 'filename="Marchen.epub"' in v and "filename*=UTF-8''M%C3%A4rchen.epub" in v
