"""Authentication invariants. Each test pins a hole that was open in production."""
from conftest import ALICE, LIB

FEED = f"/opds/alice/libraries/{LIB}/items"


def test_no_credentials_gets_basic_challenge(client):
    r = client.get(FEED)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic ")


def test_no_credentials_after_another_login_still_challenged(client):
    """Anonymous requests used to be served with the last logged-in user's token."""
    assert client.get(FEED, auth=ALICE).status_code == 200
    assert client.get(FEED).status_code == 401


def test_wrong_password_rejected_even_when_token_cached(client):
    """The token cache was keyed by username, so any password worked once cached."""
    assert client.get(FEED, auth=ALICE).status_code == 200
    r = client.get(FEED, auth=("alice", "WRONG"))
    assert r.status_code == 401


def test_correct_password_is_cached(client, fake_abs):
    for _ in range(3):
        assert client.get(FEED, auth=ALICE).status_code == 200
    assert fake_abs.logins == ["alice"]


def test_token_query_param_is_not_credentials(client):
    client.get(FEED, auth=ALICE)
    assert client.get(FEED + "?token=tok-alice").status_code == 401


def test_x_username_header_cannot_select_another_user(client):
    client.get(FEED, auth=ALICE)
    r = client.get(f"/opds/alice/libraries/{LIB}/items", auth=("bob", "bob-pw"),
                   headers={"X-Username": "alice"}, follow_redirects=False)
    # bob is redirected to his own feed, never served alice's
    assert r.status_code == 307
    assert r.headers["location"].startswith("/opds/bob/")


def test_feeds_never_embed_tokens(client):
    for path in (f"/opds/alice", f"/opds/alice/libraries/{LIB}", FEED,
                 f"/opds/alice/libraries/{LIB}/search?q=dune"):
        body = client.get(path, auth=ALICE).text
        assert "tok-alice" not in body, path
        assert "token=" not in body, path
