"""Search: the OpenSearch description must parse, and its template must work."""
from lxml import etree

from conftest import ALICE, LIB

OSD_NS = "{http://a9.com/-/spec/opensearch/1.1/}"
ATOM = "{http://www.w3.org/2005/Atom}"


def _osd(client, **kw):
    r = client.get(f"/opds/alice/libraries/{LIB}/search.xml", **kw)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/opensearchdescription+xml")
    return etree.fromstring(r.content)  # raises if not well-formed


def test_feed_advertises_search(client):
    feed = etree.fromstring(client.get(f"/opds/alice/libraries/{LIB}", auth=ALICE).content)
    links = [l for l in feed.iter("{*}link") if l.get("rel") == "search"]
    assert links and links[0].get("type") == "application/opensearchdescription+xml"


def test_description_is_well_formed_when_authenticated(client):
    """Authenticated rendering used to append a raw &token=, which does not parse."""
    _osd(client, auth=ALICE)


def test_description_template_is_absolute_and_tokenless(client):
    url = _osd(client, auth=ALICE).find(f"{OSD_NS}Url")
    template = url.get("template")
    assert template == f"http://testserver/opds/alice/libraries/{LIB}/search?q={{searchTerms}}"
    assert "kind=acquisition" in url.get("type")


def test_description_honours_forwarded_proto(client):
    url = _osd(client, headers={"X-Forwarded-Proto": "https", "Host": "kindle.example.com"})
    assert url.find(f"{OSD_NS}Url").get("template").startswith("https://kindle.example.com/")


def test_search_via_template_returns_matches(client):
    template = _osd(client).find(f"{OSD_NS}Url").get("template")
    r = client.get(template.replace("{searchTerms}", "harry%20potter"), auth=ALICE)
    assert r.status_code == 200
    titles = [t.text for t in etree.fromstring(r.content).iter("{*}entry") for t in t.iter("{*}title")]
    assert titles == ["Harry Potter and the Prisoner of Azkaban"]


def test_search_redirect_preserves_encoded_query(client):
    """Redirect to the canonical username used to rebuild q= unencoded."""
    r = client.get(f"/opds/ALICE/libraries/{LIB}/search?q=harry%20%26%20potter",
                   auth=ALICE, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/opds/alice/libraries/{LIB}/search?q=harry%20%26%20potter"


def test_search_with_markup_in_titles_is_well_formed(client):
    r = client.get(f"/opds/alice/libraries/{LIB}/search?q=ampersand", auth=ALICE)
    feed = etree.fromstring(r.content)
    assert [e for e in feed.iter("{*}entry")]


def test_description_template_encodes_odd_usernames(client):
    r = client.get(f"/opds/a%20b%3Fc/libraries/{LIB}/search.xml")
    template = etree.fromstring(r.content).find(f"{OSD_NS}Url").get("template")
    assert template == f"http://testserver/opds/a%20b%3Fc/libraries/{LIB}/search?q={{searchTerms}}"


def test_redirect_keeps_other_segments_encoded(client):
    r = client.get("/opds/ALICE/libraries/a%20b/items", auth=ALICE, follow_redirects=False)
    assert r.headers["location"] == "/opds/alice/libraries/a%20b/items"
