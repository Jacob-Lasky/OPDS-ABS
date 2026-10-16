#!/usr/bin/env python3
"""Walk a live opds-abs end to end, the way a reader would, and fail loudly.

A single well-formed page proves nothing here: every past failure returned 200.
This follows rel="next" to exhaustion on each paginated feed and checks that
pages differ, the walk terminates, entry counts match opensearch:totalResults,
the OpenSearch description parses and its template returns results, and auth
rejects missing and wrong credentials.

Credentials come from the environment, never argv (argv is visible in `ps`):
    OPDS_USER / OPDS_PASSWORD   HTTP Basic (the real reader path), or
    OPDS_BEARER                 an ABS token, when the server has API_KEY_AUTH_ENABLED

Usage:
    OPDS_USER=alice OPDS_PASSWORD=... python3 scripts/walk_feeds.py \\
        --base https://kindle.example.com --library <id> --search "harry potter"
"""
import argparse
import base64
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from xml.etree import ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"
FAILURES = []


def fail(msg):
    FAILURES.append(msg)
    print(f"FAIL  {msg}")


def auth_header():
    if os.environ.get("OPDS_USER") and os.environ.get("OPDS_PASSWORD"):
        raw = f"{os.environ['OPDS_USER']}:{os.environ['OPDS_PASSWORD']}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if os.environ.get("OPDS_BEARER"):
        return {"Authorization": "Bearer " + os.environ["OPDS_BEARER"]}
    sys.exit("set OPDS_USER/OPDS_PASSWORD or OPDS_BEARER")


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers, r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(), url


def walk(base, path, headers, label):
    url, seen, entries, pages, total = urllib.parse.urljoin(base, path), set(), [], 0, None
    while url:
        if url in seen:
            fail(f"{label}: next link loops back to {url}")
            break
        seen.add(url)
        status, _, body, final = get(url, headers)
        if status != 200:
            fail(f"{label}: HTTP {status} on {url}")
            break
        feed = ET.fromstring(body)
        pages += 1
        if total is None and feed.find(f"{OS}totalResults") is not None:
            total = int(feed.find(f"{OS}totalResults").text)
        entries += [e.findtext(f"{ATOM}id") for e in feed.findall(f"{ATOM}entry")]
        nxt = [l.get("href") for l in feed.findall(f"{ATOM}link") if l.get("rel") == "next"]
        url = urllib.parse.urljoin(final, nxt[0]) if nxt else None
    if len(entries) != len(set(entries)):
        fail(f"{label}: {len(entries) - len(set(entries))} duplicate entries across pages")
    if total is not None and total != len(entries):
        fail(f"{label}: collected {len(entries)} entries but totalResults={total}")
    print(f"ok    {label}: {len(entries)} entries over {pages} page(s)")
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--library", required=True)
    ap.add_argument("--search", default="harry potter")
    args = ap.parse_args()
    headers = auth_header()
    base = args.base.rstrip("/") + "/"

    status, _, body, final = get(base + "opds", headers)
    if status != 200:
        sys.exit(f"FAIL  /opds -> HTTP {status}; check credentials")
    user = urllib.parse.urlparse(final).path.split("/")[2]
    lib = f"opds/{user}/libraries/{args.library}"
    print(f"ok    authenticated as {user}")

    for feed in ("items", "authors", "series", "collections"):
        walk(base, f"{lib}/{feed}", headers, feed)

    status, hdrs, body, _ = get(f"{base}{lib}/search.xml")
    ctype = hdrs.get("Content-Type", "")
    if status != 200 or not ctype.startswith("application/opensearchdescription+xml"):
        fail(f"search.xml: HTTP {status}, Content-Type {ctype}")
    try:
        template = ET.fromstring(body).find(f"{OS}Url").get("template")
    except ET.ParseError as e:
        fail(f"search.xml does not parse: {e}")
        template = None
    if template:
        if "token=" in template:
            fail("search.xml template carries a token")
        q = urllib.parse.quote(args.search)
        hits = walk(base, template.replace("{searchTerms}", q), headers, f"search {args.search!r}")
        if not hits:
            fail(f"search {args.search!r} returned no entries")

    status, *_ = get(f"{base}{lib}/items")
    if status != 401:
        fail(f"no credentials -> HTTP {status}, expected 401")
    else:
        print("ok    no credentials -> 401")
    wrong = {"Authorization": "Basic " + base64.b64encode(f"{user}:definitely-wrong".encode()).decode()}
    status, *_ = get(f"{base}{lib}/items", wrong)
    if status != 401:
        fail(f"wrong password -> HTTP {status}, expected 401")
    else:
        print("ok    wrong password -> 401")

    print("PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
