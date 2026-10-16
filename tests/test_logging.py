"""The access log is the main diagnostic; it must be useful and must not leak secrets."""
import logging

from conftest import ALICE, LIB


def test_access_line_has_user_auth_ua_and_redacts_token(client, caplog):
    caplog.set_level(logging.INFO, logger="opds_abs")
    client.get(f"/opds/alice/libraries/{LIB}/search?q=dune&token=SECRET", auth=ALICE,
               headers={"User-Agent": "Marvin/3.6"})
    lines = [r.getMessage() for r in caplog.records if r.name == "opds_abs.access"]
    assert len(lines) == 1
    line = lines[0]
    assert "-> 200" in line and "user=alice" in line and 'ua="Marvin/3.6"' in line
    assert "q=dune" in line and "SECRET" not in line and "token=REDACTED" in line
    assert "auth=password" in line


def test_search_logs_terms_and_counts(client, caplog):
    caplog.set_level(logging.INFO, logger="opds_abs")
    client.get(f"/opds/alice/libraries/{LIB}/search?q=dune", auth=ALICE)
    assert any("Search q='dune'" in r.getMessage() and "1 entries" in r.getMessage()
               for r in caplog.records)


def test_no_secret_in_any_log_record(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.get(f"/opds/alice/libraries/{LIB}/items", auth=ALICE)
    client.get(f"/opds/alice/libraries/{LIB}/items", auth=("alice", "WRONG"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("alice-pw", "tok-alice", "WRONG"):
        assert secret not in text


def test_response_carries_request_id(client):
    r = client.get("/opds/alice", auth=ALICE)
    assert len(r.headers["x-request-id"]) == 8
